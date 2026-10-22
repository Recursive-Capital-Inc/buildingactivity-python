"""The `q` texts of the three search tools say how matching works (proposal #42)."""
import json

from buildingactivity.mcp import tools as T


def _prop(tool, name):
    t = next(t for t in T.list_tools() if t["name"] == tool)
    return t["inputSchema"]["properties"][name]


def test_search_tools_describe_the_q_rules():
    sig = _prop("search_signals", "q")["description"]
    assert "every word" in sig and "phrase" in sig
    prj = _prop("search_projects", "q")["description"]
    assert "every word" in prj and "no quotes" in prj
    per = _prop("search_permits", "q")["description"]
    assert "all words" in per and "phrase" in per


def test_tool_json_sizes_stay_under_budget():
    tools = T.list_tools()
    assert len(json.dumps(tools)) < 16000
    for t in tools:
        assert len(json.dumps(t)) < 2000, t["name"]


def test_q_is_passed_through_unchanged():
    calls = []

    class Api:
        has_key = True

        def get(self, path, params=None):
            calls.append((path, dict(params or {})))
            return T.Reply(200, {"items": []}, 0, "req")

    for name, path in (("search_permits", "/v1/permits/search"),
                       ("search_signals", "/v1/signals"),
                       ("search_projects", "/v1/projects")):
        calls.clear()
        payload, err = T.call_tool(name, {"geo_id": "CA", "q": 'data "center" close'},
                                   Api())
        assert err is False
        assert calls[0][0] == path
        assert calls[0][1]["q"] == 'data "center" close'
        assert payload["items"] == []
