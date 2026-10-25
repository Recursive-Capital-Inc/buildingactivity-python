"""MCP server: tool schemas, every tool against a fixture server, the token budget, error
passthrough, key handling and the transports.

The fixture server answers with recorded /v1 200 bodies (tests/fixtures/responses.json:
real API responses with build-only fields removed), so the tools are checked against the
API's actual shapes. Transport tests need the optional `mcp` SDK (skipped without it).
"""
import copy
import json
import pathlib
import re

import pytest
import requests

from buildingactivity.cli import main as cli
from buildingactivity.mcp import tools as T

KEY = "k-mcp-secret-123"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "responses.json"
EXAMPLES = json.loads(FIXTURES.read_text())
RECORD_TOOLS = {"search_permits", "get_permit", "search_projects", "get_project",
                "search_decisions", "search_signals", "get_signal"}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _routes():
    out = []
    for key in EXAMPLES:
        method, path = key.split(" ", 1)
        if method != "GET":
            continue
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((path.count("{"), rx, key))
    return sorted(out)


ROUTES = _routes()


class FixtureServer:
    """requests-style session answering GETs with the recorded example for the route."""

    def __init__(self, pad_to=None, status=None, body=None):
        self.calls, self.pad_to, self.status, self.body = [], pad_to, status, body

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        self.calls.append((path, dict(params or {}), dict(headers or {})))
        if self.status:
            return Resp(self.status, self.body, {"X-Request-Id": "req_" + "b" * 32,
                                                 "X-Records-Request": "0"})
        if headers.get("X-API-Key") != KEY:
            return Resp(401, {"error": {"type": "auth_error", "message": "bad key",
                                        "param": None}, "request_id": "req_" + "c" * 32})
        key = next(k for _, rx, k in ROUTES if re.match(rx, path))
        body = copy.deepcopy(EXAMPLES[key]["body"])
        rows = body.get("items") if "items" in body else body.get("data")
        if self.pad_to and isinstance(rows, list) and rows:
            rows[:] = [_long(copy.deepcopy(rows[i % len(rows)])) for i in range(self.pad_to)]
        n = len(rows) if isinstance(rows, list) else 1
        return Resp(200, body, {"X-Records-Request": str(n), "X-Request-Id": "req_" + "a" * 32})


def _long(row):
    """Worst-case text: every string field 400 characters long."""
    for k, v in row.items():
        if isinstance(v, str) and k not in ("id", "source_url"):
            row[k] = (v + " ") * (400 // (len(v) + 1) + 1)
    return row


def api(session=None, key=KEY):
    return T.Api("http://fixture", key, session or FixtureServer())


# One call per tool, with the arguments an agent would send.
CALLS = {
    "resolve_address": {"address": "50 Barbaree Way, Tiburon, CA"},
    "get_property": {"id": "prp_vuoc6a72qoqzzmubcrva"},
    "search_permits": {"address_id": "adr_vuoc6a72qoqzzmubcrva"},
    "get_permit": {"id": "pmt_dzptshzmc6mlibuavdrq"},
    "permit_history": {"id": "pmt_eb6tcugeh4nd4wbpaf2a"},
    "search_contractors": {"geo_id": "cty_06041", "tags": ["plumbing"]},
    "get_contractor": {"id": "ctr_73devkcy7vpjbrgu77vq"},
    "geo_lookup": {"q": "Marin", "state": "CA"},
    "metrics": {"geo_id": "cty_06041", "from": "2026-07", "to": "2026-09"},
    "coverage": {"geo_id": "cty_06041"},
    "search_projects": {"geo_id": "msa_31080", "min_value": 50000000},
    "get_project": {"id": "prj_drxydx7ulmdev7qg2ona"},
    "search_decisions": {"geo_id": "cty_48113", "category": ["rezoning"]},
    "search_signals": {"geo_id": "cty_06037", "category": ["ceqa_nod"]},
    "get_signal": {"id": "sig_lwzmzsozu2zgi2tbki6q"},
    "recent_changes": {"geo_id": "cty_04013", "tags": ["solar"], "types": ["created"]},
    "list_tags": {},
    "my_usage": {},
}
PATHS = {
    "resolve_address": "/v1/addresses/search", "get_property": "/v1/properties/prp_",
    "search_permits": "/v1/permits/search", "get_permit": "/v1/permits/pmt_",
    "permit_history": "/v1/permits/pmt_eb6tcugeh4nd4wbpaf2a/history",
    "search_contractors": "/v1/contractors/search", "get_contractor": "/v1/contractors/ctr_",
    "geo_lookup": "/v1/geo/search", "metrics": "/v1/metrics", "coverage": "/v1/meta/coverage",
    "search_projects": "/v1/projects", "get_project": "/v1/projects/prj_",
    "search_decisions": "/v1/decisions/search", "search_signals": "/v1/signals",
    "get_signal": "/v1/signals/sig_", "recent_changes": "/v1/changes",
    "list_tags": "/v1/lists/tags", "my_usage": "/v1/usage",
}


# ---- schemas ---------------------------------------------------------------------------

def test_eighteen_tools_with_short_closed_schemas():
    tools = T.list_tools()
    assert len(tools) == 18 and set(CALLS) == {t["name"] for t in tools}
    for t in tools:
        s = t["inputSchema"]
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert set(s.get("required", ())) <= set(s["properties"])
        assert len(t["description"]) <= 200, t["name"]
        assert len(json.dumps(t)) < 2400, t["name"]  # about 600 tokens
        for name, p in s["properties"].items():
            assert p.get("type") in ("string", "integer", "number", "boolean", "array"), name
    assert len(json.dumps(tools)) < 16000  # the whole tool list stays near 4,000 tokens


def test_schemas_are_valid_json_schema_and_accept_the_example_calls():
    jsonschema = pytest.importorskip("jsonschema")
    for t in T.list_tools():
        jsonschema.Draft202012Validator.check_schema(t["inputSchema"])
        jsonschema.validate(CALLS[t["name"]], t["inputSchema"])


def test_argument_checks_follow_the_schema():
    a = api()
    for args, param in [({"address": "1 Main", "bogus": 1}, "bogus"), ({}, "address"),
                        ({"address": "1 Main", "limit": 0}, "limit"),
                        ({"address": "1 Main", "limit": "5"}, "limit"),
                        ({"address": "1 Main", "limit": True}, "limit")]:
        out, err = T.call_tool("resolve_address", args, a)
        assert err and out["error"]["type"] == "validation_error"
        assert out["error"]["param"] == param and out["records_charged"] == 0
    out, err = T.call_tool("search_permits", {"status": ["nope"]}, a)
    assert err and out["error"]["param"] == "status"
    out, err = T.call_tool("nope", {}, a)
    assert err and out["error"]["type"] == "not_found"


def _param(path, name):
    for p in cli.load_spec()["paths"][path]["get"].get("parameters", []):
        if p["name"] == name:
            return p
    raise AssertionError(f"{path} has no parameter {name}")


def _enum(path, name):
    s = cli._value_schema(_param(path, name)["schema"])
    return set((cli._value_schema(s["items"]) if s.get("type") == "array" else s)["enum"])


def test_enums_match_the_bundled_spec():
    assert set(T.PERMIT_SORTS) == _enum("/v1/permits/search", "sort")
    assert set(T.PERMIT_STATUSES) == _enum("/v1/permits/search", "status")
    assert set(T.PERMIT_CLASSES) == _enum("/v1/permits/search", "category")
    # contractor sort has no enum in the spec, only a description naming the values
    desc = _param("/v1/contractors/search", "sort")["description"]
    assert set(T.CONTRACTOR_SORTS) == set(re.findall(r"[a-z]+_[a-z_]+", desc))
    assert set(T.PROJECT_SORTS) == _enum("/v1/projects", "sort")
    assert set(T.SIGNAL_SOURCES) == _enum("/v1/signals", "source")


# The /v1 routes each tool may call, and tool arguments that are not API parameters.
ROUTES_OF = {
    "resolve_address": ["/v1/addresses/search"],
    "get_property": ["/v1/properties/{id}", "/v1/properties/lookup", "/v1/properties/near"],
    "search_permits": ["/v1/permits/search"], "get_permit": ["/v1/permits/{permit_id}"],
    "permit_history": ["/v1/permits/{permit_id}/history"],
    "search_contractors": ["/v1/contractors/search", "/v1/contractors/lookup"],
    "get_contractor": ["/v1/contractors/{contractor_id}"], "geo_lookup": ["/v1/geo/search"],
    "metrics": ["/v1/metrics"], "coverage": ["/v1/meta/coverage"],
    "search_projects": ["/v1/projects"], "get_project": ["/v1/projects/{project_id}",
                                                          "/v1/projects/batch"],
    "search_decisions": ["/v1/decisions/search"], "search_signals": ["/v1/signals"],
    "get_signal": ["/v1/signals/{signal_id}", "/v1/signals/batch"],
    "recent_changes": ["/v1/changes", "/v1/webhooks/{webhook_id}"],
    "list_tags": ["/v1/lists/tags"], "my_usage": ["/v1/usage"],
}
NOT_PARAMS = {("resolve_address", "address"), ("get_property", "id"),
              ("get_project", "items"), ("get_project", "ids"),
              ("get_signal", "ids"), ("recent_changes", "webhook_id")}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_tool_argument_is_a_parameter_of_its_route(name):
    """The MCP tools are hand-written, the CLI is generated: this keeps the tools honest
    when specsync brings in a changed API (a renamed or removed parameter fails here)."""
    paths = cli.load_spec()["paths"]
    params = {x["name"] for r in ROUTES_OF[name] for x in paths[r]["get"].get("parameters", [])
              if x["in"] == "query"}
    for arg in T.TOOLS[name].schema["properties"]:
        if (name, arg) not in NOT_PARAMS and not (arg == "id" and "{" in ROUTES_OF[name][0]):
            assert arg in params, (name, arg, sorted(params))


# ---- every tool against the fixture server ---------------------------------------------

@pytest.mark.parametrize("name", sorted(CALLS))
def test_each_tool_against_the_fixture_server(name):
    srv = FixtureServer()
    out, err = T.call_tool(name, CALLS[name], api(srv))
    assert not err, out
    assert len(srv.calls) == 1  # one HTTP call per tool
    path, _params, headers = srv.calls[0]
    assert path.startswith(PATHS[name]) and headers["X-API-Key"] == KEY
    assert isinstance(out["records_charged"], int)
    rows = out["items"] if "items" in out else [out["item"]]
    assert rows and all(isinstance(r, dict) and r for r in rows)
    if "items" in out:
        assert "next_cursor" in out
    if name in RECORD_TOOLS:
        assert all("source_url" in r for r in rows)
        assert any(r["source_url"] for r in rows)
    if name == "get_project":
        assert all("source_url" in i for i in out["item"]["items"])


def test_search_permits_sends_the_filters_and_returns_compact_rows():
    srv = FixtureServer()
    out, _ = T.call_tool("search_permits", {"address_id": "adr_x", "tags": ["solar", "-pool"],
                                            "status": ["issued", "finaled"],
                                            "include_count": True, "cursor": "abc"}, api(srv))
    params = srv.calls[0][1]
    assert params == {"address_id": "adr_x", "tags": "solar,-pool", "status": "issued,finaled",
                      "include_count": "true", "cursor": "abc", "sort": "issued_desc",
                      "limit": 10}
    row = out["items"][0]
    assert row["id"].startswith("pmt_") and row["source_url"].startswith("https://")
    assert "provenance" not in row and "first_seen" not in row
    assert out["next_cursor"] == EXAMPLES["GET /v1/permits/search"]["body"]["next_cursor"]
    assert out["records_charged"] == len(out["items"])
    assert out["total_count"] == {"value": 8, "relation": "eq"}


def test_get_property_ways_in():
    srv = FixtureServer()
    a = api(srv)
    out, err = T.call_tool("get_property", {"apn": "05506128", "county_fips": "06041"}, a)
    assert not err and srv.calls[-1][0] == "/v1/properties/lookup" and out["items"]
    out, err = T.call_tool("get_property", {"lat": 37.89, "lon": -122.5}, a)
    assert not err and srv.calls[-1][0] == "/v1/properties/near"
    out, err = T.call_tool("get_property", {"id": "prp_a", "address": "x"}, a)
    assert err and out["error"]["type"] == "validation_error"
    out, err = T.call_tool("get_property", {"lat": 1.0}, a)
    assert err and out["error"]["param"] == "lon"


def test_contractor_license_goes_to_lookup():
    srv = FixtureServer()
    out, err = T.call_tool("search_contractors", {"license": "215233", "state": "CA"}, api(srv))
    assert not err and srv.calls[0][0] == "/v1/contractors/lookup"
    assert srv.calls[0][1] == {"license": "215233", "state": "CA"}
    assert out["items"][0]["license"] == "215233"
    # a license lookup cannot honour search filters, so it refuses them instead of dropping them
    srv = FixtureServer()
    out, err = T.call_tool("search_contractors", {"license": "215233", "geo_id": "cty_06041"},
                           api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "geo_id" and not srv.calls


# ---- token budget ----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CALLS))
def test_default_page_stays_under_the_token_budget(name):
    """Default page sizes, every row present and every text field at its worst case."""
    srv = FixtureServer(pad_to=50)
    out, err = T.call_tool(name, CALLS[name], api(srv))
    assert not err, out
    limit = srv.calls[0][1].get("limit")
    if "items" in out and limit:
        assert len(out["items"]) <= 50
    text = T.to_text(out if not limit else {**out, "items": out["items"][:limit]})
    assert T.estimate_tokens(text) < T.TOKEN_BUDGET, (name, T.estimate_tokens(text))


def test_text_is_clipped():
    srv = FixtureServer(pad_to=10)
    out, _ = T.call_tool("search_permits", {}, api(srv))
    assert all(len(r.get("description") or "") <= T.TEXT_MAX for r in out["items"])


# ---- errors and the key -----------------------------------------------------------------

def test_api_errors_pass_through_in_the_v1_shape():
    body = {"error": {"type": "credit_exhausted", "message": "no records left",
                      "param": None, "doc_url": "/docs/api/v1.md"},
            "request_id": "req_" + "b" * 32}
    out, err = T.call_tool("get_permit", {"id": "pmt_x"}, api(FixtureServer(status=402, body=body)))
    assert err and out == {"error": {"type": "credit_exhausted", "message": "no records left",
                                     "param": None}, "status": 402,
                           "request_id": "req_" + "b" * 32, "records_charged": 0}
    out, err = T.call_tool("get_permit", {"id": "pmt_x"}, api(FixtureServer(status=502)))
    assert err and out["error"]["type"] == "server_error" and out["status"] == 502


def test_wrong_key_is_auth_error_and_missing_key_never_calls(monkeypatch):
    out, err = T.call_tool("get_permit", {"id": "pmt_x"}, api(key="wrong"))
    assert err and out["error"]["type"] == "auth_error" and out["status"] == 401
    srv = FixtureServer()
    monkeypatch.delenv(T.ENV_KEY, raising=False)
    out, err = T.call_tool("get_permit", {"id": "pmt_x"}, T.Api("http://fixture", None, srv))
    assert err and out["error"]["type"] == "auth_error" and not srv.calls


def test_unreachable_api_is_unavailable():
    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused " + KEY)

    out, err = T.call_tool("geo_lookup", {"q": "Marin"}, api(Down()))
    assert err and out["error"]["type"] == "unavailable"
    assert KEY not in T.to_text(out)


def test_key_never_appears_in_answers_or_repr(monkeypatch):
    monkeypatch.setenv(T.ENV_KEY, KEY)
    monkeypatch.setenv(T.ENV_URL, "http://fixture/")
    a = T.Api(session=FixtureServer())
    assert a.base_url == "http://fixture" and KEY not in repr(a) and "set" in repr(a)
    for name, args in CALLS.items():
        out, _ = T.call_tool(name, args, a)
        assert KEY not in T.to_text(out)


def test_ids_are_quoted_into_the_path():
    srv = FixtureServer(status=404, body={"error": {"type": "not_found", "message": "no", "param": None}})
    T.call_tool("get_permit", {"id": "../usage?x=1"}, api(srv))
    assert srv.calls[0][0] == "/v1/permits/..%2Fusage%3Fx%3D1"


# ---- the new tools: signals, changes, tags, usage ---------------------------------------

def test_signals_carry_stage_size_and_source_url():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"geo_id": "cty_06037", "large_only": True,
                                              "min_value": 1e6, "source": ["ceqanet"]},
                           api(srv))
    assert not err, out
    assert srv.calls[0][1] == {"geo_id": "cty_06037", "large_only": "true", "min_value": 1e6,
                               "source": "ceqanet", "limit": 10}
    row = out["items"][0]
    assert row["id"].startswith("sig_") and row["stage"] and row["source_url"].startswith("https://")
    out, err = T.call_tool("get_signal", {"id": "sig_lwzmzsozu2zgi2tbki6q"}, api())
    item = out["item"]
    assert item["size"] == {"units": 120.0, "sqft": 80472.0, "acres": 3.13}
    assert item["apns"] == ["646-100-7100"] and item["project_id"].startswith("prj_")


def test_recent_changes_rows_and_resume_cursor():
    srv = FixtureServer()
    out, err = T.call_tool("recent_changes", {"geo_id": "cty_04013", "tags": ["solar"],
                                              "since": "2026-10-15"}, api(srv))
    assert not err, out
    assert srv.calls[0][1] == {"geo_id": "cty_04013", "tags": "solar",
                               "since": "2026-10-15", "limit": 10}
    row = out["items"][0]
    assert row["type"] == "created" and row["permit_id"].startswith("pmt_")
    assert row["source_url"].startswith("https://") and row["address"]
    body = EXAMPLES["GET /v1/changes"]["body"]
    assert out["next_cursor"] == body["next_cursor"] and out["has_more"] is True
    assert out["published_through"] == body["meta"]["published_through"]


def test_recent_changes_reuses_a_saved_webhook_filter():
    srv = FixtureServer()
    out, err = T.call_tool("recent_changes", {"webhook_id": "whk_4627a6423a8334e1c711",
                                              "limit": 5}, api(srv))
    assert not err, out
    assert [c[0] for c in srv.calls] == ["/v1/webhooks/whk_4627a6423a8334e1c711",
                                         "/v1/changes"]
    saved = EXAMPLES["GET /v1/webhooks/{webhook_id}"]["body"]["filter"]
    assert srv.calls[1][1] == {"geo_id": saved["geo_id"], "tags": ",".join(saved["tags"]),
                               "types": ",".join(saved["types"]), "backfill": "false",
                               "limit": 5}
    # an explicit argument wins over the saved filter
    T.call_tool("recent_changes", {"webhook_id": "whk_x", "geo_id": "CA"}, api(srv))
    assert srv.calls[-1][1]["geo_id"] == "CA"


def test_list_tags_and_my_usage():
    out, err = T.call_tool("list_tags", {}, api())
    assert not err and out["items"][0] == {
        "id": "new_construction", "description": out["items"][0]["description"],
        "status": "reliable", "permits": 2945896}
    out, err = T.call_tool("my_usage", {}, api())
    assert not err
    assert out["item"]["used"] == 0 and out["item"]["reset_date"] == "2026-11-01"
    assert "remaining" in out["item"] and "limit" in out["item"]  # null = no monthly cap


# ---- transports (optional mcp SDK) -------------------------------------------------------

def _texts(result):
    return json.loads(result.content[0].text)


def test_sdk_server_lists_and_calls_tools_in_process():
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client

    from buildingactivity.mcp import server

    async def go():
        async with Client(server.build_server(api())) as c:
            listed = await c.list_tools()
            names = {t.name for t in listed.tools}
            ok = await c.call_tool("geo_lookup", {"q": "Marin"})
            bad = await c.call_tool("get_permit", {})
            return names, ok, bad

    names, ok, bad = anyio.run(go)
    assert names == set(CALLS)
    assert not ok.is_error and _texts(ok)["items"][0]["id"] == "cty_06041"
    assert bad.is_error and _texts(bad)["error"]["param"] == "id"


def test_http_transport_uses_the_callers_key_and_refuses_none():
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from buildingactivity.mcp import server

    srv = FixtureServer()
    app = server.http_app(T.Api("http://fixture", "env-key-not-used", srv))
    hdr = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "geo_lookup", "arguments": {"q": "Marin"}}}
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.post("/mcp", json=init, headers=hdr).status_code == 401
        r = c.post("/mcp", json=init, headers={**hdr, "X-API-Key": KEY})
        assert r.status_code == 200, r.text
        r = c.post("/mcp", json=call, headers={**hdr, "Authorization": f"Bearer {KEY}"})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
    assert not result.get("isError") and "cty_06041" in result["content"][0]["text"]
    assert srv.calls[-1][2]["X-API-Key"] == KEY  # the caller's key, not the env key


def test_request_key_reads_both_headers():
    from buildingactivity.mcp.server import request_key

    assert request_key({"x-api-key": " k1 "}) == "k1"
    assert request_key({"authorization": "Bearer k2"}) == "k2"
    assert request_key({"authorization": "Basic k3"}) is None
    assert request_key({}) is None and request_key(None) is None


HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
       "MCP-Protocol-Version": "2025-06-18"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}}
CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "my_usage", "arguments": {}}}


def test_http_key_in_the_url_path_for_clients_without_headers():
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from buildingactivity.mcp import server

    srv = FixtureServer()
    app = server.http_app(T.Api("http://fixture", None, srv))
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.post(f"/mcp/{KEY}", json=CALL, headers=HDR)
        assert r.status_code == 200, r.text
        assert not r.json()["result"].get("isError")
        # a header key is replaced by the path key, never merged
        r = c.post(f"/mcp/{KEY}", json=CALL, headers={**HDR, "X-API-Key": "other"})
        assert r.status_code == 200
        assert c.post("/mcp/a/b", json=CALL, headers=HDR).status_code in (401, 404)
        assert c.post("/mcp/k\u00e9y\u4e2d", json=CALL, headers=HDR).status_code == 401
        assert c.get("/healthz").json()["ok"] is True
    assert {call[2]["X-API-Key"] for call in srv.calls} == {KEY}


def test_hosted_allowed_hosts_refuse_other_host_headers():
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from buildingactivity.mcp import server

    def app():  # one app per client: the SDK session manager runs once per instance
        return server.http_app(T.Api("http://fixture", None, FixtureServer()),
                               host="0.0.0.0", allowed_hosts=["mcp.buildingactivity.com"])

    hdr = {**HDR, "X-API-Key": KEY}
    with TestClient(app(), base_url="https://mcp.buildingactivity.com") as c:
        assert c.post("/mcp", json=CALL, headers=hdr).status_code == 200
    with TestClient(app(), base_url="https://evil.example") as c:
        assert c.post("/mcp", json=CALL, headers=hdr).status_code in (400, 421)

