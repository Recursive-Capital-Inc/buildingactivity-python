"""buildingactivity: commands generated from the bundled /v1 OpenAPI snapshot.

    buildingactivity permits search --geo-id cty_06041 --tags solar --limit all
    buildingactivity permits get pmt_dzptshzmc6mlibuavdrq
    buildingactivity property --apn 05506128 --county-fips 06041
    buildingactivity schema permits search          (offline)
    buildingactivity changes tail                   (follows /v1/changes)

Command names come from the path: the literal segments after /v1, plus `get` when the
path ends in an ID (`/v1/permits/{permit_id}` is `permits get ID`). When one command
would be both a leaf and a group (`/v1/geo/states` and `/v1/geo/states/{code}`), the
collection leaf is named `list`, or `batch` when it takes `id` (`/v1/permits?id=`).
Query parameters become `--dashed-flags`; path parameters are positional.

Output is JSON on stdout; errors are JSON on stderr. Exit codes: 0 ok, 1 client error,
2 auth, 3 rate limited (after retries), 4 credits exhausted, 5 server or network.
The key comes from BUILDINGACTIVITY_API_KEY or the config file and is never printed.
"""
import argparse
import email.utils
import getpass
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from importlib import resources
from urllib.parse import quote

EXIT_OK, EXIT_CLIENT, EXIT_AUTH, EXIT_RATE, EXIT_CREDITS, EXIT_SERVER = range(6)
ENV_KEY = "BUILDINGACTIVITY_API_KEY"
ENV_URL = "BUILDINGACTIVITY_API_URL"
ENV_CONFIG = "BUILDINGACTIVITY_CONFIG"
DEFAULT_URL = "https://api.buildingactivity.com"
PROG = "buildingactivity"
SPEC_FILE = "spec.json"
V1 = "/v1"
MAX_RETRY_WAIT = 300.0
CHANGES_KEY = "GET /v1/changes"

_sleep = time.sleep  # tests replace this


class CliError(Exception):
    def __init__(self, message, code=EXIT_CLIENT, error_type="client_error", **extra):
        super().__init__(message)
        self.message, self.code, self.error_type, self.extra = message, code, error_type, extra

    def payload(self):
        return {"error": self.message, "code": self.code, "error_type": self.error_type,
                **{k: v for k, v in self.extra.items() if v is not None}}


# ---- spec ------------------------------------------------------------------------------

def _refs(node, out):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)
    return out


def snapshot(spec):
    """The part of an /openapi.json the CLI bundles: /v1 paths and the schemas they use."""
    paths = {p: ops for p, ops in spec.get("paths", {}).items()
             if p == V1 or p.startswith(V1 + "/")}
    schemas = spec.get("components", {}).get("schemas", {})
    used, todo = set(), _refs(paths, set())
    while todo - used:
        name = (todo - used).pop()
        used.add(name)
        _refs(schemas.get(name, {}), todo)
    return {"openapi": spec.get("openapi"), "info": spec.get("info", {}), "paths": paths,
            "components": {"schemas": {k: schemas[k] for k in sorted(used) if k in schemas}}}


def load_spec():
    return json.loads(resources.files(__package__).joinpath(SPEC_FILE).read_text())


def _value_schema(schema):
    for branch in schema.get("anyOf") or ():
        if branch.get("type") != "null":
            return branch
    return schema


@dataclass
class Param:
    name: str
    where: str  # query or path
    type: str  # string, integer, number, boolean, array
    required: bool = False
    enum: list | None = None
    item_type: str = "string"
    maximum: float | None = None
    default: object = None
    description: str = ""
    example: object = None

    @property
    def flag(self):
        return "--" + self.name.replace("_", "-")


@dataclass
class Op:
    key: str
    method: str
    path: str
    summary: str = ""
    description: str = ""
    params: list = field(default_factory=list)
    words: tuple = ()

    @property
    def paginated(self):
        return any(p.name == "cursor" for p in self.params)

    def param(self, name):
        return next((p for p in self.params if p.name == name), None)

    @property
    def command(self):
        return " ".join(self.words)


def _param(raw):
    schema = raw.get("schema", {})
    s = _value_schema(schema)
    type_ = s.get("type", "string")
    item = _value_schema(s.get("items", {})) if type_ == "array" else {}
    enum = item.get("enum") if type_ == "array" else s.get("enum")
    return Param(name=raw["name"], where=raw["in"], type=type_, required=raw.get("required", False),
                 enum=enum, item_type=item.get("type", "string"), maximum=s.get("maximum"),
                 default=schema.get("default", s.get("default")),
                 description=(raw.get("description") or schema.get("description") or "").strip(),
                 example=raw.get("example"))


def operations(spec):
    """{command words: Op} for every GET under /v1, named by the module docstring rule."""
    ops = []
    for path, methods in spec.get("paths", {}).items():
        for method, raw in methods.items():
            if method != "get" or not isinstance(raw, dict):
                continue
            segs = [s for s in path[len(V1):].split("/") if s]
            words = [s for s in segs if not s.startswith("{")]
            if segs and segs[-1].startswith("{"):
                words.append("get")
            op = Op(key=f"GET {path}", method="GET", path=path, summary=raw.get("summary", ""),
                    description=raw.get("description", ""),
                    params=[_param(p) for p in raw.get("parameters", [])
                            if p.get("in") in ("query", "path")], words=tuple(words))
            ops.append(op)
    for op in ops:
        if op.words == ("changes",):  # `changes` is a group: `changes tail` is built in
            op.words = ("changes", "list")
    prefixes = {op.words[:i] for op in ops for i in range(1, len(op.words))}
    for op in ops:
        if op.words in prefixes:
            op.words = op.words + (("batch",) if op.param("id") else ("list",))
    out = {}
    for op in ops:
        if op.words in out:
            raise ValueError(f"two operations map to `{op.command}`: {out[op.words].key}, {op.key}")
        out[op.words] = op
    return out


# ---- config and key ----------------------------------------------------------------------

def config_path():
    if os.environ.get(ENV_CONFIG):
        return os.environ[ENV_CONFIG]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "buildingactivity", "config.json")


def read_config():
    try:
        with open(config_path()) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read config file {config_path()}: {exc}") from None
    return data if isinstance(data, dict) else {}


def write_config(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.chmod(path, 0o600)


def api_key():
    """(key, source) or (None, None). Environment first, then the config file."""
    if os.environ.get(ENV_KEY):
        return os.environ[ENV_KEY], "env"
    key = read_config().get("api_key")
    return (key, "config") if key else (None, None)


def base_url(arg=None):
    url = arg or os.environ.get(ENV_URL) or read_config().get("base_url") or DEFAULT_URL
    return url.rstrip("/")


# ---- HTTP ---------------------------------------------------------------------------------

def _session():
    import requests
    return requests.Session()


def _retry_after(resp, attempt):
    value = (resp.headers.get("Retry-After") or "").strip()
    if value:
        try:
            return min(max(float(value), 0.0), MAX_RETRY_WAIT)
        except ValueError:
            when = email.utils.parsedate_to_datetime(value)
            if when is not None:
                return min(max(when.timestamp() - time.time(), 0.0), MAX_RETRY_WAIT)
    return min(2 ** attempt + random.random(), MAX_RETRY_WAIT)


def _api_error(resp):
    try:
        body = resp.json()
    except ValueError:
        return {}, None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"], body.get("request_id")
    if isinstance(body, dict) and "detail" in body:
        return {"message": str(body["detail"])}, None
    return {}, None


def _fail(resp, url):
    status = resp.status_code
    err, rid = _api_error(resp)
    msg = err.get("message") or f"HTTP {status}"
    if status in (401, 403):
        code, kind = EXIT_AUTH, "auth_error"
    elif status == 402:
        code, kind = EXIT_CREDITS, "credit_exhausted"
    elif status == 429:
        code, kind = EXIT_RATE, "rate_limited"
    elif status >= 500:
        code, kind = EXIT_SERVER, "server_error"
    else:
        code, kind = EXIT_CLIENT, "validation_error" if status == 422 else "client_error"
    return CliError(msg, code, kind, status=status, api_error_type=err.get("type"),
                    param=err.get("param"), request_id=rid, url=url)


def request(session, url, params, key, opts):
    """GET with retries on 429 (Retry-After honored). Returns the decoded JSON body."""
    import requests
    attempt = 0
    while True:
        try:
            resp = session.get(url, params=params, headers={"X-API-Key": key,
                                                            "Accept": "application/json"},
                               timeout=opts.timeout)
        except requests.RequestException as exc:
            raise CliError(f"{type(exc).__name__}: cannot reach {url}", EXIT_SERVER,
                           "network_error", url=url) from None
        if resp.status_code == 429 and attempt < opts.retries:
            _sleep(_retry_after(resp, attempt))
            attempt += 1
            continue
        if resp.status_code != 200:
            err = _fail(resp, url)
            if resp.status_code == 429:
                err.message = f"rate limited after {attempt} retries: {err.message}"
            raise err
        try:
            return resp.json()
        except ValueError:
            raise CliError(f"response from {url} is not JSON", EXIT_SERVER, "server_error",
                           url=url) from None


def prepared_url(url, params):
    import requests
    return requests.Request("GET", url, params=params).prepare().url


# ---- arguments ------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    """argparse that raises CliError (exit 1, JSON on stderr) instead of exiting 2."""

    def error(self, message):
        raise CliError(f"{self.prog}: {message}", EXIT_CLIENT, "client_error")


def _bool(text):
    t = str(text).strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def _limit(text):
    if text == "all":
        return "all"
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer or 'all'") from None
    if n < 1:
        raise argparse.ArgumentTypeError("expected a positive integer or 'all'")
    return n


def _positive(text):
    n = _limit(text)
    if n == "all":
        raise argparse.ArgumentTypeError("expected a positive integer")
    return n


_TYPES = {"integer": int, "number": float, "boolean": _bool, "string": str}


def common_options():
    p = Parser(add_help=False)
    g = p.add_argument_group("common options")
    g.add_argument("--base-url", help=f"API root (env {ENV_URL}, config base_url, default "
                   f"{DEFAULT_URL})")
    g.add_argument("--limit", type=_limit, help="total items: a number, or 'all' to follow "
                   "next_cursor to the end (list commands)")
    g.add_argument("--max-records", type=_positive,
                   help="never fetch more than this many records in total (caps --limit all)")
    g.add_argument("--page-size", type=_positive, help="items per request (paginated commands)")
    g.add_argument("--retries", type=int, default=3, help="retries on HTTP 429 (default 3)")
    g.add_argument("--no-retry", action="store_true", help="fail at once on HTTP 429")
    g.add_argument("--timeout", type=float, default=30.0, help="seconds per request")
    g.add_argument("--dry-run", action="store_true", help="print the request URL; no call")
    g.add_argument("--compact", action="store_true", help="one-line JSON output")
    return p


def _help(param):
    text = param.description or param.name
    if param.enum:
        text += f" One of: {', '.join(map(str, param.enum))}."
    if param.example is not None:
        ex = param.example
        text += f" Example: {','.join(map(str, ex)) if isinstance(ex, list) else ex}."
    return text.replace("%", "%%")


def add_op_arguments(parser, op):
    for p in op.params:
        if p.where == "path":
            parser.add_argument(p.name, metavar=p.name.upper(), help=_help(p))
        elif p.name in ("limit", "cursor"):
            continue  # driven by --limit, --page-size and pagination
        elif p.type == "array":
            parser.add_argument(p.flag, dest="q_" + p.name, action="append",
                                metavar=p.item_type.upper(), required=p.required,
                                help=_help(p) + " Repeat or comma-separate.")
        elif p.type == "boolean":
            parser.add_argument(p.flag, dest="q_" + p.name, type=_bool, nargs="?", const=True,
                                metavar="true|false", required=p.required, help=_help(p))
        else:
            parser.add_argument(p.flag, dest="q_" + p.name, type=_TYPES.get(p.type, str),
                                metavar=p.name.upper(), required=p.required, choices=p.enum,
                                help=_help(p))
    if op.param("cursor"):
        parser.add_argument("--cursor", dest="q_cursor", help="start from this next_cursor")


def build_parser(spec=None):
    spec = load_spec() if spec is None else spec
    ops = operations(spec)
    common = common_options()
    root = Parser(prog=PROG, description="BuildingActivity API v1 from the command line. "
                  "JSON on stdout, JSON errors on stderr; see docs/cli.md.")
    root.add_argument("--version", action="version",
                      version=f"{PROG} (spec {spec.get('info', {}).get('version', '?')})")
    top = root.add_subparsers(dest="cmd", metavar="COMMAND")
    groups = {(): top}
    for words in sorted(ops):
        op = ops[words]
        for i in range(1, len(words)):
            prefix = words[:i]
            if prefix not in groups:
                g = groups[prefix[:-1]].add_parser(prefix[-1], help=f"{' '.join(prefix)} commands")
                groups[prefix] = g.add_subparsers(dest=f"cmd{i}", metavar="COMMAND",
                                                  required=True)
        leaf = groups[words[:-1]].add_parser(words[-1], parents=[common], help=op.summary,
                                             description=(op.description or op.summary))
        leaf.set_defaults(op=op)
        add_op_arguments(leaf, op)
    _add_extra(top, groups, common, ops, spec)
    return root


def _add_extra(top, groups, common, ops, spec):
    s = top.add_parser("schema", help="show commands or one command's parameters (offline)")
    s.add_argument("words", nargs="*", help="a command, e.g. permits search")
    s.add_argument("--compact", action="store_true")
    s.set_defaults(run=run_schema, spec=spec)

    c = top.add_parser("config", help="where the key and base URL come from")
    cs = c.add_subparsers(dest="config_cmd", metavar="COMMAND", required=True)
    show = cs.add_parser("show", help="config file, base URL, whether a key is set (never "
                         "the key)")
    show.add_argument("--compact", action="store_true")
    cs.add_parser("set-key", help="store the API key in the config file (read from stdin)")
    u = cs.add_parser("set-url", help="store the API root in the config file")
    u.add_argument("url")
    c.set_defaults(run=run_config)

    p = top.add_parser("property", parents=[common], help="find a property: by ID, address "
                       "text, parcel (APN + county FIPS) or coordinates")
    p.add_argument("id", nargs="?", help="prp_ or adr_ ID")
    p.add_argument("--address", help="free-text address")
    p.add_argument("--city")
    p.add_argument("--state")
    p.add_argument("--zip")
    p.add_argument("--apn", help="assessor parcel number (with --county-fips)")
    p.add_argument("--county-fips", help="five-digit state and county FIPS")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--radius-m", type=float, help="meters, with --lat/--lon")
    p.set_defaults(run=run_property, ops=ops)

    chs = groups.get(("changes",))  # the spec's own /v1/changes... commands, if any
    if chs is None:
        ch = top.add_parser("changes", help="follow the change feed (/v1/changes)")
        chs = ch.add_subparsers(dest="changes_cmd", metavar="COMMAND", required=True)
    t = chs.add_parser("tail", parents=[common], help="print new changes as JSON lines")
    t.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    t.add_argument("--once", action="store_true", help="stop when caught up")
    changes = next((o for o in ops.values() if o.key == CHANGES_KEY), None)
    if changes is not None:
        add_op_arguments(t, changes)
    t.set_defaults(run=run_changes_tail, changes_op=changes)


# ---- running ---------------------------------------------------------------------------------

def _query(args, op):
    q = {}
    for p in op.params:
        if p.where != "query":
            continue
        v = getattr(args, "q_" + p.name, None)
        if v is None:
            continue
        if p.type == "array":
            v = [x.strip() for item in v for x in str(item).split(",") if x.strip()]
            bad = [x for x in v if p.enum and x not in p.enum]
            if bad:
                raise CliError(f"{p.flag}: {', '.join(bad)} not one of {', '.join(p.enum)}")
        elif p.type == "boolean":
            v = "true" if v else "false"
        q[p.name] = v
    return q


def _url(base, op, args):
    path = op.path
    for p in op.params:
        if p.where == "path":
            path = path.replace("{" + p.name + "}", quote(str(getattr(args, p.name)), safe=":"))
    return base + path


def _list_key(body):
    for k in ("items", "data"):
        if isinstance(body, dict) and isinstance(body.get(k), list):
            return k
    return None


def _caps(args):
    total = None if args.limit in (None, "all") else args.limit
    if args.max_records is not None:
        total = args.max_records if total is None else min(total, args.max_records)
    return total


def _page_size(op, args, remaining):
    """The API `limit` for the next request, or None for the API default."""
    lim = op.param("limit")
    if lim is None:
        return None
    api_max = int(lim.maximum) if lim.maximum else None
    if op.paginated:
        size = args.page_size or (api_max if args.limit == "all" else None)
        if remaining is not None:
            size = min(size, remaining) if size else remaining
    else:
        size = api_max if args.limit == "all" else remaining
    return min(size, api_max) if size and api_max else size


def _need_key():
    key, _src = api_key()
    if not key:
        raise CliError(f"API key is required. Set {ENV_KEY} or run: {PROG} config set-key",
                       EXIT_AUTH, "auth_error")
    return key


def execute(op, args, query, out=sys.stdout):
    """Run one generated command; follow next_cursor when --limit asks for more."""
    if args.no_retry:
        args.retries = 0
    if (args.limit is not None or args.max_records is not None) and not op.param("limit"):
        raise CliError(f"`{op.command}` returns one result; --limit and --max-records apply "
                       "to list commands")
    url = _url(base_url(args.base_url), op, args)
    total = _caps(args)
    q = dict(query)
    size = _page_size(op, args, total)
    if size:
        q["limit"] = size
    if args.dry_run:
        print(prepared_url(url, q), file=out)
        return EXIT_OK
    key = _need_key()
    session = _session()
    body = request(session, url, q, key, args)
    lk = _list_key(body)
    if op.paginated and args.limit is not None and lk:
        items = list(body[lk])[:total] if total else list(body[lk])
        pages, nxt = 1, body.get("next_cursor")
        while nxt and (total is None or len(items) < total):
            remaining = None if total is None else total - len(items)
            q = {**query, "cursor": nxt}
            size = _page_size(op, args, remaining)
            if size:
                q["limit"] = size
            page = request(session, url, q, key, args)
            got = page.get(lk) or []
            items.extend(got[:remaining] if remaining else got)
            pages, nxt = pages + 1, page.get("next_cursor")
            if not got:
                break
        body = {**body, lk: items, "next_cursor": nxt}
        if "size" in body:
            body["size"] = len(items)
        body["cli"] = {"pages": pages, "records": len(items)}
    _emit(body, args, out)
    return EXIT_OK


def _emit(body, args, out):
    if getattr(args, "compact", False):
        out.write(json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n")
    else:
        out.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")


def run_schema(args, out=sys.stdout):
    ops = operations(args.spec)
    if not args.words:
        body = [{"command": op.command, "method": op.method, "path": op.path,
                 "summary": op.summary, "paginated": op.paginated,
                 "params": [p.name for p in op.params]} for op in ops.values()]
        _emit(sorted(body, key=lambda b: b["command"]), args, out)
        return EXIT_OK
    op = ops.get(tuple(args.words))
    if op is None:
        raise CliError(f"no command `{' '.join(args.words)}`; run `{PROG} schema` for the list")
    raw = args.spec["paths"][op.path]["get"]
    _emit({"command": op.command, "method": op.method, "path": op.path,
           "summary": op.summary, "description": op.description,
           "parameters": raw.get("parameters", []), "flags": {
               p.name: (p.flag if p.where == "query" else "positional") for p in op.params},
           "responses": sorted(raw.get("responses", {}))}, args, out)
    return EXIT_OK


def run_config(args, out=sys.stdout):
    if args.config_cmd == "show":
        key, src = api_key()
        _emit({"config_file": config_path(), "base_url": base_url(),
               "api_key": f"set (from {'env ' + ENV_KEY if src == 'env' else 'config file'})"
               if key else "not set"}, args, out)
        return EXIT_OK
    data = read_config()
    if args.config_cmd == "set-url":
        data["base_url"] = args.url.rstrip("/")
    else:
        key = (getpass.getpass("API key: ") if sys.stdin.isatty()
               else sys.stdin.readline()).strip()
        if not key:
            raise CliError("no key given on stdin")
        data["api_key"] = key
    write_config(data)
    _emit({"config_file": config_path(), "saved": args.config_cmd.removeprefix("set-")},
          args, out)
    return EXIT_OK


def run_property(args, out=sys.stdout):
    ops = args.ops
    by_key = {o.key: o for o in ops.values()}
    if args.id:
        op, query = by_key["GET /v1/properties/{id}"], {}
    elif args.apn or args.address:
        op = by_key["GET /v1/properties/lookup"]
        if args.apn and not args.county_fips:
            raise CliError("--apn needs --county-fips (APNs repeat across counties)")
        query = {k: v for k, v in (("address", args.address), ("city", args.city),
                                   ("state", args.state), ("zip", args.zip), ("apn", args.apn),
                                   ("county_fips", args.county_fips)) if v is not None}
    elif args.lat is not None and args.lon is not None:
        op = by_key["GET /v1/properties/near"]
        query = {"lat": args.lat, "lon": args.lon}
        if args.radius_m is not None:
            query["radius_m"] = args.radius_m
    else:
        raise CliError("give a property ID, --address, --apn with --county-fips, or --lat "
                       "with --lon")
    return execute(op, args, query, out)


def run_changes_tail(args, out=sys.stdout):
    """Print each change as one JSON line; at the end of the feed wait --interval and poll
    again from the last cursor (or stop with --once). --max-records stops after n."""
    op = args.changes_op
    if op is None:
        raise CliError("this API has no /v1/changes feed yet (it ships with the status "
                       "history and change feed release); nothing to tail", EXIT_CLIENT,
                       "unsupported")
    if args.no_retry:
        args.retries = 0
    url = _url(base_url(args.base_url), op, args)
    query = _query(args, op)
    cursor = query.pop("cursor", None)
    if args.page_size:
        query["limit"] = args.page_size
    if args.dry_run:
        print(prepared_url(url, dict(query, **({"cursor": cursor} if cursor else {}))),
              file=out)
        return EXIT_OK
    key = _need_key()
    session = _session()
    seen = 0
    while True:
        body = request(session, url, dict(query, **({"cursor": cursor} if cursor else {})),
                       key, args)
        items = body.get(_list_key(body) or "items") or []
        for item in items:
            out.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n")
            seen += 1
            if args.max_records and seen >= args.max_records:
                out.flush()
                return EXIT_OK
        out.flush()
        nxt = body.get("next_cursor")
        # has_more, when the API sends it, is the authority: a feed page can be empty
        # (a narrow filter over a scan window) while more events follow
        more = bool(nxt) and (body["has_more"] if "has_more" in body else bool(items))
        cursor = nxt or cursor
        if not more:
            if args.once:
                return EXIT_OK
            _sleep(args.interval)


def run(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if getattr(args, "run", None):
            return args.run(args, out)
        if getattr(args, "op", None) is None:
            parser.print_help(out)
            return EXIT_OK
        return execute(args.op, args, _query(args, args.op), out)
    except CliError as exc:
        err.write(json.dumps(exc.payload(), ensure_ascii=False) + "\n")
        return exc.code
    except KeyboardInterrupt:
        return EXIT_OK


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
