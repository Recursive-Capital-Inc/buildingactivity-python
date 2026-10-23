"""The search_projects type filter and the type field on project rows (proposal #37)."""
import re

from buildingactivity.cli import main as cli
from buildingactivity.mcp import tools as T
from test_mcp_tools import FixtureServer, api


def _spec_type_desc():
    param = next(x for x in cli.load_spec()["paths"]["/v1/projects"]["get"]["parameters"]
                 if x["name"] == "type")
    return param["description"].split(":", 1)[1]


def test_project_types_match_the_bundled_spec_description():
    listed = [t.split()[0].rstrip(".") for t in _spec_type_desc().split(",")]
    assert list(T.PROJECT_TYPES) == listed
    assert all(re.fullmatch(r"[a-z_]+", t) for t in listed)


def _body(row):
    return {"items": [row], "next_cursor": None}


def test_search_projects_sends_the_type_filter():
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {"type": ["data_center", "hospital"]}, api(srv))
    assert not err, out
    assert srv.calls[0][1]["type"] == "data_center,hospital"


def test_search_projects_without_type_sends_no_type_param():
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert "type" not in srv.calls[0][1]


def test_rows_carry_type_from_the_apis_type_object():
    srv = FixtureServer(status=200, body=_body(
        {"id": "prj_x", "type": {"type": "data_center", "basis": ["name"],
                                 "evidence_item_id": "sig_1"}, "source_urls": ["https://a"]}))
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert out["items"][0]["type"] == "data_center"
    assert out["items"][0]["source_url"] == "https://a"


def test_rows_without_a_type_have_no_type_key():
    srv = FixtureServer(status=200, body=_body({"id": "prj_x", "source_urls": ["https://a"]}))
    out, err = T.call_tool("search_projects", {}, api(srv))
    assert not err, out
    assert "type" not in out["items"][0]


def test_unknown_type_is_a_validation_error_before_any_call():
    srv = FixtureServer()
    out, err = T.call_tool("search_projects", {"type": ["data_center", "nope"]}, api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "type" and out["records_charged"] == 0
    assert not srv.calls


def test_get_project_item_carries_type():
    srv = FixtureServer(status=200, body={
        "id": "prj_x", "type": {"type": "hospital", "basis": [], "evidence_item_id": None},
        "items": []})
    out, err = T.call_tool("get_project", {"id": "prj_x"}, api(srv))
    assert not err, out
    assert out["item"]["type"] == "hospital"


def test_project_row_keeps_a_plain_string_type():
    row = T.project_row({"id": "prj_x", "type": "data_center"})
    assert row["type"] == "data_center"
