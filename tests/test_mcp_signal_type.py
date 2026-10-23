"""search_signals takes and returns `type`, and PROJECT_TYPES follows the API's 12 types
(proposal #37)."""
from buildingactivity.mcp import tools as T
from test_mcp_tools import FixtureServer, api


def test_project_types_follow_the_api():
    assert T.PROJECT_TYPES == ("data_center", "hospital", "stadium_arena", "school",
                               "logistics_industrial", "mixed_use", "multifamily", "office",
                               "solar_farm", "energy_storage", "infrastructure", "other")


def test_search_projects_accepts_the_new_types():
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {"geo_id": "msa_x", "type": ["energy_storage"]},
                           api(srv))
    assert not err, out
    assert srv.calls[0][1]["type"] == "energy_storage"


def test_search_signals_sends_type():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"geo_id": "cty_x", "type": ["data_center"]},
                           api(srv))
    assert not err, out
    assert srv.calls[0][1]["type"] == "data_center"


def test_search_signals_without_type_sends_none():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"geo_id": "cty_x"}, api(srv))
    assert not err, out
    assert "type" not in srv.calls[0][1]


def test_search_signals_rejects_unknown_type_before_any_call():
    srv = FixtureServer()
    out, err = T.call_tool("search_signals", {"geo_id": "cty_x", "type": ["nope"]}, api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "type" and out["records_charged"] == 0 and not srv.calls


def _body(row):
    return {"items": [row], "next_cursor": None}


def test_signal_rows_carry_type_from_the_api_row():
    srv = FixtureServer(status=200, body=_body({"id": "sig_x", "type": "data_center",
                                                "source_url": "https://a"}))
    out, err = T.call_tool("search_signals", {}, api(srv))
    assert not err, out
    assert out["items"][0]["type"] == "data_center"


def test_signal_rows_without_a_type_have_no_type_key():
    srv = FixtureServer(status=200, body=_body({"id": "sig_x", "source_url": "https://a"}))
    out, err = T.call_tool("search_signals", {}, api(srv))
    assert not err, out
    assert "type" not in out["items"][0]
