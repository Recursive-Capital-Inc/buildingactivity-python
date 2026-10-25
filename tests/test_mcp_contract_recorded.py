"""Contract tests against recorded live REST answers (tests/fixtures/mcp_contract_recorded/,
2026-10-25, owner key): the MCP rows and tool text must match what the API actually sends.

Each fixture file is {"request", "status", "body"}; the fake api object feeds the recorded
body to the real tool code, like FixtureServer in tests/test_mcp_tools.py.
"""
import json
import pathlib

from buildingactivity.mcp import tools as T

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "mcp_contract_recorded"


class RecordedApi:
    """Answers every GET with the recorded body of one fixture file."""

    has_key = True

    def __init__(self, *names):
        self.names = list(names)
        self.calls = []
        self.bodies = [json.loads((FIXTURES / f"{n}.json").read_text()) for n in names]

    def get(self, path, params=None):
        # record what the wire would carry (T.Api drops None and empty params)
        sent = {k: v for k, v in (params or {}).items() if v is not None and v != []}
        self.calls.append((path, sent))
        rec = self.bodies[len(self.calls) - 1]
        rows = rec["body"].get("items") if isinstance(rec["body"], dict) else None
        n = len(rows) if isinstance(rows, list) else 1
        return T.Reply(rec["status"], rec["body"], n, "req_" + "a" * 32)


def norm(s):
    return " ".join(s.split()) if isinstance(s, str) else s


def call(name, args, rec):
    out, err = T.call_tool(name, args, rec)
    assert not err, out
    return out


# ---- R2A-6: get_permit is the detail tool, its description is not clipped to 160 --------

def test_get_permit_keeps_the_rest_description():
    rec = RecordedApi("permit_pmt_2jw4evhwe3cybmovtsxq")
    out = call("get_permit", {"id": "pmt_2jw4evhwe3cybmovtsxq"}, rec)
    rest = json.loads((FIXTURES / "permit_pmt_2jw4evhwe3cybmovtsxq.json").read_text())["body"]
    assert out["item"]["description"] == norm(rest["description"])
    assert len(out["item"]["description"]) > T.TEXT_MAX


def test_get_permit_keeps_a_276_char_description():
    rec = RecordedApi("permit_pmt_nsmbfmqy6knf7xwlgqla")
    out = call("get_permit", {"id": "pmt_nsmbfmqy6knf7xwlgqla"}, rec)
    rest = json.loads((FIXTURES / "permit_pmt_nsmbfmqy6knf7xwlgqla.json").read_text())["body"]
    assert out["item"]["description"] == norm(rest["description"])


# ---- R2A-5: record_date is a permit row date --------------------------------------------

def test_get_permit_carries_record_date():
    rec = RecordedApi("permit_pmt_2jw4evhwe3cybmovtsxq")
    out = call("get_permit", {"id": "pmt_2jw4evhwe3cybmovtsxq"}, rec)
    rest = json.loads((FIXTURES / "permit_pmt_2jw4evhwe3cybmovtsxq.json").read_text())["body"]
    assert out["item"]["record_date"] == rest["record_date"]


def test_search_rows_with_only_record_date_show_a_date():
    rec = RecordedApi("search_sacramento_record_date")
    out = call("search_permits", {"geo_id": "jur_06-067-64000-00000",
                                  "date_from": "2026-10-20", "date_to": "2026-10-24",
                                  "sort": "first_seen_desc"}, rec)
    rest = json.loads((FIXTURES / "search_sacramento_record_date.json").read_text())["body"]
    dates = ("applied", "issued", "finaled", "record_date")
    for rest_row, row in zip(rest["items"], out["items"]):
        if any(rest_row.get(k) for k in ("date_applied", "date_issued", "date_finaled",
                                         "record_date")):
            assert any(row.get(k) for k in dates), rest_row["id"]
    # search rows stay compact: descriptions still clipped to TEXT_MAX
    assert all(len(r.get("description") or "") <= T.TEXT_MAX for r in out["items"])


# ---- R2G-6: metrics without a window asks for the latest 36 months ----------------------

def test_metrics_without_a_window_sends_the_last_36_months(monkeypatch):
    import datetime

    monkeypatch.setattr(T, "utcnow",
                        lambda: datetime.datetime(2026, 10, 25, tzinfo=datetime.UTC))
    rec = RecordedApi("metrics_CA_default")
    call("metrics", {"geo_id": "CA"}, rec)
    params = rec.calls[0][1]
    assert params["from"] == "2023-11" and params["to"] == "2026-10"


def test_metrics_with_a_window_sends_just_what_it_got():
    rec = RecordedApi("metrics_CA_default")
    call("metrics", {"geo_id": "CA", "from": "2026-01"}, rec)
    params = rec.calls[0][1]
    assert params["from"] == "2026-01" and "to" not in params


# ---- R2B-10 / R2G-13: tool text ----------------------------------------------------------

def _schema(name):
    return next(t for t in T.list_tools() if t["name"] == name)["inputSchema"]


def test_metrics_coverage_and_contractors_geo_id_rejects_a_zip():
    for name in ("metrics", "coverage", "search_contractors"):
        desc = _schema(name)["properties"]["geo_id"]["description"]
        assert "ZIP" not in desc and "94920" not in desc, name


def test_every_string_property_has_a_description_and_no_san_rafael():
    for t in T.list_tools():
        for name, p in t["inputSchema"]["properties"].items():
            if p.get("type") == "string":
                assert p.get("description"), (t["name"], name)
        assert "San Rafael" not in json.dumps(t), t["name"]


# ---- R2D-8: get_project items default is honest ------------------------------------------

def test_get_project_items_schema_says_both_defaults():
    p = _schema("get_project")["properties"]["items"]
    assert "default" not in p
    assert "10" in p["description"] and "3" in p["description"] and "ids" in p["description"]


# ---- R2G-9: changed events carry no permit snapshot --------------------------------------

def test_change_row_on_recorded_changed_events():
    rec = json.loads((FIXTURES / "changes_cty_08005_changed.json").read_text())["body"]
    for e in rec["items"]:
        row = T.change_row(e)
        assert row["seq"] == e["seq"]
        assert row["type"] == "changed"
        assert row["permit_id"] == e["permit_id"]
        assert row["field"] == e["field"]
        assert row["old"] == (norm(e["old"])[:57].rstrip() + "...") or row["old"] == norm(e["old"])
        assert "new" in row


def test_recent_changes_text_says_what_changed_events_carry():
    desc = T.TOOLS["recent_changes"].description
    assert "permit_id" in desc and "get_permit" in desc
