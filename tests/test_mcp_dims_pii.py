"""metrics rows keep their group_by dimension key even when the value is null, and
get_permit's text says personal names are plan-gated.

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
    def __init__(self, body, records=1):
        self.body, self.records, self.calls = body, records, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("/" + url.split("://", 1)[-1].split("/", 1)[-1],
                           dict(params or {})))
        return Resp(200, self.body, {"X-Records-Request": str(self.records)})


def _call(name, args, fake):
    return T.call_tool(name, args, T.Api("http://fixture", KEY, fake))


def test_metrics_null_dimension_key_is_kept():
    fake = Fake({"data": [
        {"jurisdiction": None, "permits": 6504, "issued": 0},
        {"jurisdiction": "jur_08-041-99990-00000", "permits": 10},
    ], "meta": {}})
    out, err = _call("metrics", {"geo_id": "st_06", "group_by": "jurisdiction"}, fake)
    assert not err
    rows = out["items"]
    assert len(rows) == 2
    assert "jurisdiction" in rows[0] and rows[0]["jurisdiction"] is None
    assert rows[0]["permits"] == 6504
    assert rows[1]["jurisdiction"] == "jur_08-041-99990-00000"


def test_metrics_month_still_prunes_other_nulls():
    fake = Fake({"data": [
        {"month": "2026-01", "permits": 12, "value_p50": None},
    ], "meta": {}})
    out, err = _call("metrics", {"geo_id": "st_06"}, fake)
    assert not err
    row = out["items"][0]
    assert "value_p50" not in row and row["month"] == "2026-01"


def test_metrics_month_rows_carry_no_other_dimension_keys():
    fake = Fake({"data": [{"month": f"2026-{m:02d}", "permits": 3} for m in range(1, 13)] * 3,
                 "meta": {}})
    out, err = _call("metrics", {"geo_id": "st_06"}, fake)
    assert not err
    rows = out["items"]
    assert len(rows) == 36
    for row in rows:
        assert not set(row) & {"tag", "class", "status", "jurisdiction"}


def test_get_permit_description_mentions_plan_gate():
    desc = T.TOOLS["get_permit"].description
    assert "plan" in desc
