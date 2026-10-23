"""search_permits include_count always answers a total_count object (proposal #40).

Serves recorded-style /v1/permits/search bodies, no network."""

import pytest

from buildingactivity.mcp import server
from buildingactivity.mcp import tools as T

KEY = "k-mcp-secret-123"
PAGE = {"items": [{"id": "pmt_a", "source_url": "https://example.com/a"}], "next_cursor": None}


class Resp:
    def __init__(self, body):
        self.status_code, self._body = 200, body
        self.headers = {"X-Records-Request": "1", "X-Request-Id": "req_" + "a" * 32}

    def json(self):
        return self._body


class PermitServer:
    """Answers /v1/permits/search with PAGE plus the given total_count overrides."""

    def __init__(self, overrides):
        self.body = {**PAGE, **overrides}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("/" + url.split("://", 1)[-1].split("/", 1)[-1],
                           dict(params or {})))
        return Resp(self.body)


def api(srv):
    return T.Api("http://fixture", KEY, srv)


def call(srv, args):
    out, err = T.call_tool("search_permits", args, api(srv))
    assert not err, out
    return out


COUNTED = {"address_id": "adr_x", "include_count": True}


def test_null_total_count_with_estimate_becomes_unknown():
    out = call(PermitServer({"total_count": None, "total_estimate": 500}), COUNTED)
    assert out["total_count"] == {"value": None, "relation": "unknown", "reason": "timeout"}
    assert "total_estimate" not in out


def test_missing_total_count_key_becomes_unknown():
    out = call(PermitServer({}), COUNTED)
    assert out["total_count"] == {"value": None, "relation": "unknown", "reason": "timeout"}
    assert "total_estimate" not in out


@pytest.mark.parametrize("body,expected", [
    ({"total_count": {"value": 760, "relation": "eq"}}, {"value": 760, "relation": "eq"}),
    ({"total_count": {"value": 10000, "relation": "gte"}}, {"value": 10000, "relation": "gte"}),
    ({"total_count": 760}, {"value": 760, "relation": "eq"}),
])
def test_api_total_count_passes_through(body, expected):
    out = call(PermitServer(body), COUNTED)
    assert out["total_count"] == expected


@pytest.mark.parametrize("args", [{}, {"include_count": False}])
def test_without_include_count_no_total_count(args):
    args = {"address_id": "adr_x", **args}
    out = call(PermitServer({"total_count": {"value": 8, "relation": "eq"}}), args)
    assert "total_count" not in out


def test_unknown_object_still_records_charged():
    out = call(PermitServer({"total_count": None}), COUNTED)
    assert out["records_charged"] == len(out["items"])


def test_instructions_mention_unknown_and_the_cap():
    text = server.INSTRUCTIONS
    assert "unknown" in text and "10,000" in text
