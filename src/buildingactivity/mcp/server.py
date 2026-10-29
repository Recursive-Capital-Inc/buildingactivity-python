"""buildingactivity-mcp: the BuildingActivity API v1 as a small MCP server (docs/mcp.md).

    BUILDINGACTIVITY_API_KEY=... buildingactivity-mcp            (stdio, for desktop apps)
    buildingactivity-mcp --transport http --port 8765        (streamable HTTP at /mcp)

A thin client of the HTTP API: every tool is a GET to /v1 (tools.py), so auth, metering
and the v1 error shape are the API's own. stdio uses the key from
BUILDINGACTIVITY_API_KEY. Streamable HTTP uses each caller's own key, from `X-API-Key`,
`Authorization: Bearer <key>` or, for clients that cannot send headers (ChatGPT and
Claude connectors without header support), the URL path `/mcp/<key>`. Requests without a
key get 401 unless --use-env-key is given for a single-user local setup. Keys and request
paths are never logged. Each tool call and each `initialize` leaves one usage event
(usage.py: sha256 key ref, tool, outcome, latency, argument names and sizes, client name and
version; never the key or an argument value). GET /healthz answers 200 without a key. Needs the optional extra:
pip install 'buildingactivity[mcp]'.
"""
import argparse
import json
import logging
import os
import sys
import time

from buildingactivity.mcp import tools as T
from buildingactivity.mcp import usage as U

NAME = "buildingactivity"
INSTRUCTIONS = (
    "US building permits, properties, contractors, zoning decisions, early project signals "
    "and big projects from BuildingActivity. Answers are compact JSON: items, next_cursor "
    "(pass back as cursor for the next page) and records_charged (records billed for the "
    "call). Permits at an address: resolve_address, then search_permits(address_id=<id>). "
    "Area filters take a geo_id from geo_lookup; tag filters take ids from list_tags. "
    "Text search q: search_permits and search_signals need every word (whole words, no "
    "plurals) and treat \"a b\" as a phrase; search_projects matches parts of words and "
    "takes no quotes. "
    "What is new: recent_changes. Plan and records left: my_usage. Every record row has "
    "source_url, the issuer's own record. records_charged counts records new to your "
    "organization this UTC calendar month: one per permit, signal, project, contractor, "
    "property, address or decision row, and one per metrics row. Reading the same record "
    "again this month is free from any tool (search_signals then get_signal charges once). "
    "Permit rows in your paid area feed, geo_lookup, coverage, list_tags, my_usage and errors are free."
    " search_permits include_count adds total_count {value, relation}: eq is exact, gte means"
    " at least 10,000, unknown (value null) means counting ran out of time; retry once or"
    " narrow the filters."
)
ENV_ALLOWED_HOSTS = "BUILDINGACTIVITY_MCP_ALLOWED_HOSTS"
# Set only by RequireKey (any copy a client sends is dropped): how the key arrived.
KEY_VIA_HEADER = "x-buildingactivity-key-via"
INIT_BODY_CAP = 64 * 1024
log = logging.getLogger("buildingactivity.mcp")


def _version():
    try:
        from importlib.metadata import version

        return version("buildingactivity")
    except Exception:  # noqa: BLE001 - not installed as a distribution (PYTHONPATH=src)
        return "0.1.0"


def request_key(headers):
    """The caller's key from X-API-Key or `Authorization: Bearer`, else None."""
    if headers is None:
        return None
    key = headers.get("x-api-key")
    if key:
        return key.strip() or None
    auth = headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


def key_via(headers):
    """header, bearer or path: where request_key found the caller's key (None without one)."""
    if headers is None or not request_key(headers):
        return None
    via = headers.get(KEY_VIA_HEADER)
    if via == "path":
        return via
    return "header" if (headers.get("x-api-key") or "").strip() else "bearer"


def _headers(ctx):
    req = getattr(ctx, "request", None)
    return getattr(req, "headers", None)


def _tool_args(name):
    t = T.TOOLS.get(name)
    return set((t.schema or {}).get("properties") or {}) if t else set()


def build_server(api=None, *, http=False, use_env_key=False, usage=None):
    """The MCP Server. `http`: take each caller's key from its request headers. `usage`: the
    UsageSink for per-call events (default: from the environment)."""
    try:
        import anyio
        import mcp_types as types
        from mcp.server import Server
    except ImportError as exc:
        raise SystemExit("buildingactivity-mcp needs the MCP SDK: pip install "
                         "'buildingactivity[mcp]'") from exc
    api = api or T.Api()
    usage = usage or U.UsageSink.from_env()

    async def on_list_tools(ctx, params):
        ro = types.ToolAnnotations(read_only_hint=True, open_world_hint=True,
                                   destructive_hint=False, idempotent_hint=True)
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"],
                       input_schema=t["inputSchema"], annotations=ro)
            for t in T.list_tools()])

    async def on_call_tool(ctx, params):
        client, headers = api, None
        key = getattr(api, "_key", None) if api.has_key else None
        via = "env" if key else None
        if http:
            headers = _headers(ctx)
            hkey = request_key(headers)
            client = api.with_key(hkey) if hkey else (api if use_env_key else api.with_key(""))
            if hkey:
                key, via = hkey, key_via(headers)
            elif not use_env_key:
                key, via = None, None
        args = dict(params.arguments or {})
        t0 = time.monotonic()
        try:
            payload, is_error = await anyio.to_thread.run_sync(
                T.call_tool, params.name, args, client)
        except BaseException as exc:
            _record(usage, headers, key, via, params.name, args, t0, type(exc).__name__, None, None)
            raise
        err = payload.get("error") if is_error else None
        _record(usage, headers, key, via, params.name, args, t0,
                err["type"] if err else "ok", err and payload.get("status"),
                payload.get("records_charged"))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=T.to_text(payload))],
            is_error=is_error)

    return Server(NAME, version=_version(), instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _record(usage, headers, key, via, name, args, t0, outcome, status, records):
    """One tool_call event; never raises (a usage failure must not fail the tool call)."""
    try:
        h = headers or {}
        known = name in T.TOOLS
        usage.tool_call(key=key, key_via=via, tool=name if known else "unknown",
                        outcome=outcome, status=status,
                        latency_ms=round((time.monotonic() - t0) * 1000, 1),
                        records_charged=records,
                        args=U.arg_shape(args, _tool_args(name) if known else set()),
                        user_agent=h.get("user-agent"),
                        protocol_version=h.get("mcp-protocol-version"),
                        server_version=_version())
    except Exception as exc:  # noqa: BLE001
        log.warning("usage event dropped: %s", type(exc).__name__)


async def _json(send, status, body, extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *extra]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class RequireKey:
    """ASGI wrapper for the HTTP transport. GET /healthz: 200, no key. `<path>/<key>`:
    the key moves into an X-API-Key header and the path becomes `<path>` (for clients
    that can only be given a URL). Any other HTTP request without a key: 401."""

    def __init__(self, app, path="/mcp", require=True, usage=None):
        self.app, self.path, self.require = app, path.rstrip("/"), require
        self.usage = usage

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path") or ""
            if path == "/healthz":
                await _json(send, 200, {"ok": True, "version": _version()})
                return
            key = path[len(self.path) + 1:].strip("/") if path.startswith(
                self.path + "/") else ""
            try:
                key_bytes = key.encode("ascii") if key and "/" not in key else b""
            except UnicodeEncodeError:  # no real key is non-ASCII: treat as no key
                key_bytes, scope = b"", dict(scope, path=self.path + "/invalid")
            via_name = KEY_VIA_HEADER.encode()
            kept = [h for h in scope.get("headers") or [] if h[0].lower() != via_name]
            if key_bytes:
                scope = dict(scope, path=self.path, raw_path=self.path.encode(),
                             headers=[*(h for h in kept if h[0].lower() != b"x-api-key"),
                                      (b"x-api-key", key_bytes), (via_name, b"path")])
            else:
                scope = dict(scope, headers=kept)
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers") or []}
            if self.require and not request_key(headers):
                await _json(send, 401, {"error": {
                    "type": "auth_error", "param": None,
                    "message": "send your BuildingActivity API key as X-API-Key, "
                               "Authorization: Bearer <key>, or in the URL as "
                               f"{self.path}/<key>"}},
                            [(b"www-authenticate", b"Bearer")])
                return
            if (self.usage is not None and scope.get("method") == "POST"
                    and scope.get("path") == self.path and request_key(headers)):
                receive = await self._watch_initialize(headers, receive)
        await self.app(scope, receive, send)

    async def _watch_initialize(self, headers, receive):
        """Buffer the POST body (up to INIT_BODY_CAP), record any JSON-RPC `initialize` in it,
        and return a receive that replays the buffered messages unchanged. A larger body is
        passed through unparsed."""
        buffered, size, complete = [], 0, False
        while True:
            msg = await receive()
            buffered.append(msg)
            if msg.get("type") != "http.request":
                break
            size += len(msg.get("body") or b"")
            if size > INIT_BODY_CAP:
                break
            if not msg.get("more_body"):
                complete = True
                break
        if complete:
            try:
                self._record_initialize(headers, b"".join(m.get("body") or b"" for m in buffered))
            except Exception as exc:  # noqa: BLE001 - never fail a request over usage
                log.warning("usage: initialize not recorded: %s", type(exc).__name__)

        async def replay():
            if buffered:
                return buffered.pop(0)
            return await receive()

        return replay

    def _record_initialize(self, headers, body):
        if b'"initialize"' not in body:
            return
        try:
            doc = json.loads(body)
        except ValueError:
            return
        for msg in doc if isinstance(doc, list) else [doc]:
            if not isinstance(msg, dict) or msg.get("method") != "initialize":
                continue
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            self.usage.initialize(key=request_key(headers), key_via=key_via(headers),
                                  client_name=info.get("name"),
                                  client_version=info.get("version"),
                                  protocol_version=params.get("protocolVersion"),
                                  user_agent=headers.get("user-agent"))


def _security(host, allowed_hosts):
    """DNS rebinding protection: the SDK's default for a loopback bind, or an explicit
    list of public host names (the hosted service sets mcp.buildingactivity.com)."""
    if not allowed_hosts:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h for name in allowed_hosts for h in (name, name + ":*")]
    origins = [o for name in allowed_hosts for o in (f"https://{name}", f"https://{name}:*")]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


def http_app(api=None, *, use_env_key=False, host="127.0.0.1", path="/mcp",
             allowed_hosts=None, usage=None):
    usage = usage or U.UsageSink.from_env()
    server = build_server(api, http=True, use_env_key=use_env_key, usage=usage)
    app = server.streamable_http_app(streamable_http_path=path, host=host,
                                     stateless_http=True, json_response=True,
                                     transport_security=_security(host, allowed_hosts))
    return RequireKey(app, path, require=not use_env_key, usage=usage)


async def serve_stdio(api=None):
    from mcp.server.stdio import stdio_server

    server = build_server(api)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    p = argparse.ArgumentParser(prog="buildingactivity-mcp", description=__doc__.split("\n\n")[0])
    p.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    p.add_argument("--port", type=int, default=8765, help="HTTP port")
    p.add_argument("--path", default="/mcp", help="HTTP endpoint path")
    p.add_argument("--base-url", help=f"API root (default ${T.ENV_URL} or {T.DEFAULT_URL})")
    p.add_argument("--allowed-host", action="append", default=None,
                   help=f"public host name to accept (repeatable; env ${ENV_ALLOWED_HOSTS}, "
                        "comma-separated), e.g. mcp.buildingactivity.com")
    p.add_argument("--use-env-key", action="store_true",
                   help=f"HTTP: use ${T.ENV_KEY} for callers that send no key (single user)")
    args = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    # One "Terminating session" INFO line per stateless request drowned the usage lines.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    api = T.Api(base_url=args.base_url)
    log.info("buildingactivity-mcp %s transport=%s api=%s key=%s", _version(), args.transport,
             api.base_url, "set" if api.has_key else "unset")
    if args.transport == "stdio":
        import anyio

        anyio.run(serve_stdio, api)
        return 0
    import uvicorn

    allowed = args.allowed_host or [h.strip() for h in
                                    os.environ.get(ENV_ALLOWED_HOSTS, "").split(",") if h.strip()]
    # access_log off: a key sent in the URL path must never reach a log line
    uvicorn.run(http_app(api, use_env_key=args.use_env_key, host=args.host, path=args.path,
                         allowed_hosts=allowed),
                host=args.host, port=args.port, log_level="warning", access_log=False,
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
