"""metrics charges only the rows it shows; search_permits takes a permit class scope.

Fixture-only: a requests-style fake session records each GET and answers a canned body.
"""
from buildingactivity.mcp import tools as T

KEY = "k-mcp-secret-123"


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class Fake:
    def __init__(self, body, records):
        self.body, self.records, self.calls = body, records, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("/" + url.split("://", 1)[-1].split("/", 1)[-1], dict(params or {})))
        return Resp(200, self.body, {"X-Records-Request": str(self.records)})


def _rows(n):
    return [{"jurisdiction": f"j{i}", "permits": i} for i in range(n)]


def _call(name, args, fake):
    return T.call_tool(name, args, T.Api("http://fixture", KEY, fake))


def test_metrics_sends_limit_36():
    fake = Fake({"data": _rows(3), "meta": {}}, 3)
    _call("metrics", {"geo_id": "st_06", "group_by": "jurisdiction"}, fake)
    path, query = fake.calls[0]
    assert path == "/v1/metrics" and str(query["limit"]) == "36"


def test_metrics_rows_not_shown_from_rows_available():
    fake = Fake({"data": _rows(36), "meta": {"rows_available": 255}}, 36)
    out, err = _call("metrics", {"geo_id": "st_06", "group_by": "jurisdiction"}, fake)
    assert not err
    assert len(out["items"]) == 36 and out["records_charged"] == 36
    assert out["rows_not_shown"] == 219


def test_metrics_old_api_without_rows_available():
    fake = Fake({"data": _rows(40), "meta": {}}, 40)
    out, err = _call("metrics", {"geo_id": "st_06", "group_by": "jurisdiction"}, fake)
    assert not err and len(out["items"]) == 36 and out["rows_not_shown"] == 4


def _permits(args, body=None):
    fake = Fake(body or {"items": []}, 0)
    out, err = _call("search_permits", args, fake)
    return out, err, fake


def test_search_permits_category_passthrough():
    _, _, fake = _permits({"q": "food"})
    assert "category" not in fake.calls[0][1]
    _, _, fake = _permits({"q": "food", "category": ["all"]})
    assert fake.calls[0][1]["category"] == "all"
    _, _, fake = _permits({"q": "food", "category": ["environmental_health", "planning"]})
    assert fake.calls[0][1]["category"].split(",") == ["environmental_health", "planning"]


def test_search_permits_unknown_class_is_rejected_before_any_call():
    out, err, fake = _permits({"category": ["bogus"]})
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "category" and out["records_charged"] == 0
    assert fake.calls == []


def test_search_permits_passes_excluded_classes():
    out, err, _ = _permits({"q": "food"}, {"items": [], "excluded_classes": ["planning"]})
    assert not err and out["excluded_classes"] == ["planning"]
    out, _, _ = _permits({"q": "food"}, {"items": [], "excluded_classes": []})
    assert "excluded_classes" not in out


def test_descriptions_name_date_basis_and_scope():
    tools = {t["name"]: t for t in T.list_tools()}
    m, s = tools["metrics"]["description"], tools["search_permits"]["description"]
    assert "applied" in m and "36" in m and len(m) <= 200
    assert "construction" in s and "all" in s and len(s) <= 200


def test_search_permits_schema_hints():
    tools = {t["name"]: t for t in T.list_tools()}
    props = tools["search_permits"]["inputSchema"]["properties"]
    assert "cty_" in props["geo_id"]["description"] and "CA" in props["geo_id"]["description"]
    assert "YYYY-MM-DD" in props["issued_from"]["description"]
    assert "YYYY-MM-DD" in props["applied_from"]["description"]
    assert "all" in props["category"]["description"]


def test_search_permits_all_with_other_class_is_rejected_before_any_call():
    out, err, fake = _permits({"category": ["all", "planning"]})
    assert err and out["error"]["type"] == "validation_error"
    assert out["error"]["param"] == "category" and out["records_charged"] == 0
    assert fake.calls == []
