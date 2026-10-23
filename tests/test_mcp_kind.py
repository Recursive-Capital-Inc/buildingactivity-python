"""The search_signals and search_projects kind filter and the kind field on rows (proposal #36)."""
import re

from buildingactivity.cli import main as cli
from buildingactivity.mcp import tools as T
from test_mcp_tools import FixtureServer, api


def _spec_kinds(path):
    param = next(x for x in cli.load_spec()["paths"][path]["get"]["parameters"]
                 if x["name"] == "kind")
    return param["schema"]["anyOf"][0]["items"]["enum"]


def test_signal_kinds_match_the_bundled_spec():
    listed = _spec_kinds("/v1/signals")
    assert list(T.SIGNAL_KINDS) == listed
    assert all(re.fullmatch(r"[a-z_]+", k) for k in listed)


def test_project_kinds_match_the_bundled_spec():
    listed = _spec_kinds("/v1/projects")
    assert list(T.SIGNAL_KINDS) == listed
    assert all(re.fullmatch(r"[a-z_]+", k) for k in listed)


def test_kind_description_matches_and_names_all_four():
    for name in ("search_signals", "search_projects"):
        desc = T.TOOLS[name].schema["properties"]["kind"]["description"]
        assert desc == T.KIND_TEXT
        for word in ("site", "policy", "program", "other"):
            assert word in desc


def _body(row):
    return {"items": [row], "next_cursor": None}


def test_search_signals_sends_the_kind_filter():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"kind": ["policy"]}, api(srv))
    assert not err, out
    assert srv.calls[0][1]["kind"] == "policy"


def test_search_signals_sends_two_kinds_comma_joined():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"kind": ["policy", "program"]}, api(srv))
    assert not err, out
    assert srv.calls[0][1]["kind"] == "policy,program"


def test_search_projects_sends_the_kind_filter():
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {"kind": ["policy"]}, api(srv))
    assert not err, out
    assert srv.calls[0][1]["kind"] == "policy"


def test_no_kind_sends_no_kind_param():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {}, api(srv))
    assert not err, out
    assert "kind" not in srv.calls[0][1]
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert "kind" not in srv.calls[0][1]


def test_unknown_kind_is_a_validation_error_before_any_call():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"kind": ["policy", "nope"]}, api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "kind" and out["records_charged"] == 0
    assert not srv.calls


def test_signal_rows_carry_kind():
    srv = FixtureServer(status=200, body=_body({"id": "sig_x", "kind": "policy",
                                                "url": "https://a"}))
    out, err = T.call_tool("search_signals", {}, api(srv))
    assert not err, out
    assert out["items"][0]["kind"] == "policy"


def test_project_rows_carry_kind():
    srv = FixtureServer(status=200, body=_body({"id": "prj_x", "kind": "site",
                                                "source_urls": ["https://a"]}))
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert out["items"][0]["kind"] == "site"


def test_null_kind_is_pruned():
    srv = FixtureServer(status=200, body=_body({"id": "sig_x", "kind": None,
                                                "url": "https://a"}))
    out, err = T.call_tool("search_signals", {}, api(srv))
    assert not err, out
    assert "kind" not in out["items"][0]
    srv = FixtureServer(status=200, body=_body({"id": "prj_x", "kind": None,
                                                "source_urls": ["https://a"]}))
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert "kind" not in out["items"][0]


def test_get_signal_carries_kind():
    srv = FixtureServer(status=200, body={"id": "sig_x", "kind": "policy"})
    out, err = T.call_tool("get_signal", {"id": "sig_x"}, api(srv))
    assert not err, out
    assert out["item"]["kind"] == "policy"


def test_get_project_carries_kind():
    srv = FixtureServer(status=200, body={"id": "prj_x", "kind": "site", "items": []})
    out, err = T.call_tool("get_project", {"id": "prj_x"}, api(srv))
    assert not err, out
    assert out["item"]["kind"] == "site"
