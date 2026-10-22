"""buildingactivity-mcp: the BuildingActivity API v1 as a small MCP server (docs/mcp.md).

    BUILDINGACTIVITY_API_KEY=... buildingactivity-mcp            (stdio, for desktop apps)
    buildingactivity-mcp --transport http --port 8765        (streamable HTTP at /mcp)

A thin client of the HTTP API: every tool is a GET to /v1 (tools.py), so auth, metering
and the v1 error shape are the API's own. stdio uses the key from
BUILDINGACTIVITY_API_KEY. Streamable HTTP uses each caller's own key, from `X-API-Key`,
`Authorization: Bearer <key>` or, for clients that cannot send headers (ChatGPT and
Claude connectors without header support), the URL path `/mcp/<key>`. Requests without a
key get 401 unless --use-env-key is given for a single-user local setup. Keys and request
paths are never logged. GET /healthz answers 200 without a key. Needs the optional extra:
pip install 'buildingactivity[mcp]'.
"""
import argparse
import json
import logging
import os
import sys

from buildingactivity.mcp import tools as T

NAME = "buildingactivity"
INSTRUCTIONS = (
    "US building permits, properties, contractors, zoning decisions, early project signals "
    "and big projects from BuildingActivity. Answers are compact JSON: items, next_cursor "
    "(pass back as cursor for the next page) and records_charged (records billed for the "
    "call). Permits at an address: resolve_address, then search_permits(address_id=<id>). "
    "Area filters take a geo_id from geo_lookup; tag filters take ids from list_tags. "
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


def _headers(ctx):
    req = getattr(ctx, "request", None)
    return getattr(req, "headers", None)


def build_server(api=None, *, http=False, use_env_key=False):
    """The MCP Server. `http`: take each caller's key from its request headers."""
    try:
        import anyio
        import mcp_types as types
        from mcp.server import Server
    except ImportError as exc:
        raise SystemExit("buildingactivity-mcp needs the MCP SDK: pip install "
                         "'buildingactivity[mcp]'") from exc
    api = api or T.Api()

    async def on_list_tools(ctx, params):
        ro = types.ToolAnnotations(read_only_hint=True, open_world_hint=True,
                                   destructive_hint=False, idempotent_hint=True)
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"],
                       input_schema=t["inputSchema"], annotations=ro)
            for t in T.list_tools()])

    async def on_call_tool(ctx, params):
        client = api
        if http:
            key = request_key(_headers(ctx))
            client = api.with_key(key) if key else (api if use_env_key else api.with_key(""))
        payload, is_error = await anyio.to_thread.run_sync(
            T.call_tool, params.name, dict(params.arguments or {}), client)
        log.info("tool %s -> %s records=%s", params.name,
                 payload["error"]["type"] if is_error else "ok", payload.get("records_charged"))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=T.to_text(payload))],
            is_error=is_error)

    return Server(NAME, version=_version(), instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _json(send, status, body, extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *extra]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class RequireKey:
    """ASGI wrapper for the HTTP transport. GET /healthz: 200, no key. `<path>/<key>`:
    the key moves into an X-API-Key header and the path becomes `<path>` (for clients
    that can only be given a URL). Any other HTTP request without a key: 401."""

    def __init__(self, app, path="/mcp", require=True):
        self.app, self.path, self.require = app, path.rstrip("/"), require

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
            if key_bytes:
                scope = dict(scope, path=self.path, raw_path=self.path.encode(),
                             headers=[*(h for h in scope.get("headers") or []
                                        if h[0].lower() != b"x-api-key"),
                                      (b"x-api-key", key_bytes)])
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
        await self.app(scope, receive, send)


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
             allowed_hosts=None):
    server = build_server(api, http=True, use_env_key=use_env_key)
    app = server.streamable_http_app(streamable_http_path=path, host=host,
                                     stateless_http=True, json_response=True,
                                     transport_security=_security(host, allowed_hosts))
    return RequireKey(app, path, require=not use_env_key)


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
