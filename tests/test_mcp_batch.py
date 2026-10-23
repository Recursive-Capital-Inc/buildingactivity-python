"""get_signal and get_project take `ids` for several records in one call.

The batch routes of the API (GET /v1/signals/batch, GET /v1/projects/batch) answer
{"items": [...], "size": n, "meta": {...}} and charge one record per signal or project.
The tools reuse the single-id row builders, keep the single-id answer byte for byte, and
catch bad argument combinations before any call.
"""
import json

from buildingactivity.mcp import tools as T

KEY = "k-batch-secret-1"

SIG_A = {
    "id": "sig_4lvt2ihrxmtcpclnmguq", "project_id": "prj_thiu733cmhv2jdvwr7mq",
    "title": "Planned development for 420 units mixed use",
    "doc_type": "Zoning Case", "category": "planning_case", "status": "In Review",
    "filed_date": "2026-02-01", "location": "420 Main St", "description":
    "Planned development for 420 units mixed use",
    "url": "https://dallas.example/Z-25-1234", "apns": ["646-100-7100"],
    "case_numbers": ["Z-25-1234"], "units": 420.0, "size_basis": ["420 units/lots"],
    "large": True, "agency": "dallas_tx", "jurisdiction": "Dallas", "state": "TX",
}
SIG_B = {
    "id": "sig_5gkytgaxnbgm6rukvyvq", "project_id": "prj_wawtmpw4vtbhceindg3a",
    "title": "CEQA notice", "category": "ceqa_nod", "status": "Filed",
    "filed_date": "2026-03-01", "description": "A notice of determination",
    "url": "https://ceqanet.example/5", "state": "CA", "units": 100.0,
}
PRJ_A = {
    "id": "prj_bj3uxinpwmctyh6mifmq", "name": "Walker Ranch Specific Plan",
    "state": "CA", "city": "ONTARIO", "address": "1000 Walker Ave",
    "value_usd": 90000000.0, "permit_count": 2, "signal_count": 2,
    "latest_item_date": "2026-08-01", "source_urls": ["https://ontario.example/plan"],
    "stage": {"stage": "permit_issued", "date": "2026-08-01"},
    "items": [{"id": "pmt_ogeqf4pvpljlty3oyska", "kind": "permit",
               "title": "Grading permit", "date": "2026-08-01",
               "source_url": "https://ontario.example/pmt1"},
              {"id": "sig_5hcon4y2m7v2ldywmc7a", "kind": "ceqa",
               "title": "NOP", "date": "2025-05-20",
               "source_url": "https://ceqanet.example/1"}],
}
PRJ_B = {
    "id": "prj_nfqnptpw5gfinbrremyq", "name": "Second project", "state": "CA",
    "value_usd": 1000000.0, "permit_count": 1, "signal_count": 0,
    "source_urls": ["https://example.org/prj"], "stage": {"stage": "in_review"},
    "items": [],
}

SIGNAL_BATCH_BODY = {
    "items": [SIG_A, SIG_B], "size": 2,
    "meta": {"missing": ["sig_mmm"], "not_returned": ["sig_nrn"],
             "out_of_scope": ["sig_ooo"]},
}
PROJECT_BATCH_BODY = {
    "items": [PRJ_A, PRJ_B], "size": 2,
    "meta": {"missing": ["prj_mmm"], "aliased": ["prj_aliased"]},
}


class Resp:
    def __init__(self, status, body, records):
        self.status_code, self._body = status, body
        self.headers = {"X-Records-Request": str(records), "X-Request-Id": "req_batch"}

    def json(self):
        return self._body


class BatchServer:
    """requests-style session answering from a path -> (body, records) table."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        self.calls.append((path, dict(params or {}), dict(headers or {})))
        body, records = self.routes[path]
        return Resp(200, json.loads(json.dumps(body)), records)


def api(server):
    return T.Api("http://fixture", KEY, server)


def test_signal_ids_call_the_batch_route_once():
    srv = BatchServer({"/v1/signals/batch": (SIGNAL_BATCH_BODY, 2)})
    out, err = T.call_tool("get_signal", {"ids": [SIG_A["id"], SIG_B["id"]]}, api(srv))
    assert not err, out
    assert len(srv.calls) == 1
    path, params, headers = srv.calls[0]
    assert path == "/v1/signals/batch" and headers["X-API-Key"] == KEY
    assert params == {"id": SIG_A["id"] + "," + SIG_B["id"]}
    assert out["items"] and [r["id"] for r in out["items"]] == [SIG_A["id"], SIG_B["id"]]
    assert out["missing"] == ["sig_mmm"] and out["not_returned"] == ["sig_nrn"]
    assert out["out_of_scope"] == ["sig_ooo"]
    assert out["records_charged"] == 2
    assert "next_cursor" not in out and "item" not in out


def test_signal_batch_rows_equal_the_single_id_row():
    single = BatchServer({f"/v1/signals/{SIG_A['id']}": (SIG_A, 1)})
    one, err = T.call_tool("get_signal", {"id": SIG_A["id"]}, api(single))
    assert not err, one
    assert one["item"] and single.calls[0][0] == f"/v1/signals/{SIG_A['id']}"
    srv = BatchServer({"/v1/signals/batch": ({"items": [SIG_A], "size": 1, "meta": {}}, 1)})
    many, err = T.call_tool("get_signal", {"ids": [SIG_A["id"]]}, api(srv))
    assert not err, many
    assert many["items"][0] == one["item"]


def test_signal_single_id_answer_is_unchanged():
    srv = BatchServer({f"/v1/signals/{SIG_A['id']}": (SIG_A, 1)})
    out, err = T.call_tool("get_signal", {"id": SIG_A["id"]}, api(srv))
    assert not err and list(out) == ["item", "records_charged"]
    assert out["item"]["id"] == SIG_A["id"] and out["records_charged"] == 1


def test_signal_id_and_ids_together_or_neither_is_a_validation_error():
    srv = BatchServer({"/v1/signals/batch": (SIGNAL_BATCH_BODY, 2)})
    for args in ({"id": SIG_A["id"], "ids": [SIG_A["id"]]}, {}):
        out, err = T.call_tool("get_signal", args, api(srv))
        assert err and out["error"]["type"] == "validation_error"
        assert out["records_charged"] == 0
    assert not srv.calls


def test_too_many_signal_or_project_ids_is_a_validation_error_before_any_call():
    srv = BatchServer({})
    out, err = T.call_tool("get_signal", {"ids": [f"sig_{i:020d}" for i in range(21)]},
                           api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["records_charged"] == 0 and not srv.calls
    out, err = T.call_tool("get_project", {"ids": [f"prj_{i:020d}" for i in range(11)]},
                           api(srv))
    assert err and out["error"]["type"] == "validation_error"
    assert out["records_charged"] == 0 and not srv.calls


def test_project_ids_call_the_batch_route_with_three_items_each():
    srv = BatchServer({"/v1/projects/batch": (PROJECT_BATCH_BODY, 2)})
    out, err = T.call_tool("get_project", {"ids": [PRJ_A["id"], PRJ_B["id"]]}, api(srv))
    assert not err, out
    assert len(srv.calls) == 1 and srv.calls[0][0] == "/v1/projects/batch"
    assert srv.calls[0][1] == {"id": PRJ_A["id"] + "," + PRJ_B["id"]}
    assert [r["id"] for r in out["items"]] == [PRJ_A["id"], PRJ_B["id"]]
    assert len(out["items"][0]["items"]) == 2  # the fixture holds only 2 items
    assert out["missing"] == ["prj_mmm"] and out["aliased"] == ["prj_aliased"]
    assert out["records_charged"] == 2
    assert "not_returned" not in out and "next_cursor" not in out


def test_project_ids_rows_equal_the_single_id_row():
    single = BatchServer({f"/v1/projects/{PRJ_A['id']}": (PRJ_A, 1)})
    one, err = T.call_tool("get_project", {"id": PRJ_A["id"]}, api(single))
    assert not err and one["item"]["id"] == PRJ_A["id"]
    assert len(one["item"]["items"]) == 2  # 10 asked, fixture holds 2
    srv = BatchServer({"/v1/projects/batch": ({"items": [PRJ_A], "size": 1, "meta": {}}, 1)})
    many, err = T.call_tool("get_project", {"ids": [PRJ_A["id"]]}, api(srv))
    assert not err, many
    assert many["items"][0] == one["item"]


def test_project_id_and_ids_together_or_neither_is_a_validation_error():
    srv = BatchServer({"/v1/projects/batch": (PROJECT_BATCH_BODY, 2)})
    for args in ({"id": PRJ_A["id"], "ids": [PRJ_A["id"]]}, {}):
        out, err = T.call_tool("get_project", args, api(srv))
        assert err and out["error"]["type"] == "validation_error"
        assert out["records_charged"] == 0
    assert not srv.calls


def test_project_items_argument_is_honoured_with_ids():
    srv = BatchServer({"/v1/projects/batch": (PROJECT_BATCH_BODY, 2)})
    out, err = T.call_tool("get_project", {"ids": [PRJ_A["id"]], "items": 1}, api(srv))
    assert not err and [i["id"] for i in out["items"][0]["items"]] == ["pmt_ogeqf4pvpljlty3oyska"]
    assert out["items"][0]["items_not_shown"] == 1


def test_project_ids_default_to_three_items_each():
    five = json.loads(json.dumps(PRJ_A))
    five["items"] = [dict(five["items"][0], id=f"pmt_{i:020d}") for i in range(5)]
    srv = BatchServer({"/v1/projects/batch": ({"items": [five], "size": 1, "meta": {}}, 1)})
    out, err = T.call_tool("get_project", {"ids": [PRJ_A["id"]]}, api(srv))
    assert not err, out
    assert [i["id"] for i in out["items"][0]["items"]] == [f"pmt_{i:020d}" for i in range(3)]
    assert out["items"][0]["items_not_shown"] == 2


def test_ten_projects_at_three_items_stay_under_the_token_budget():
    def big_project(i):
        p = json.loads(json.dumps(PRJ_A))
        p["id"] = f"prj_{i:020d}"
        p["name"] = "Walker Ranch Specific Plan " * 20
        p["items"] = [dict(item, title=item["title"] + " words " * 30) for item in p["items"]]
        return p

    body = {"items": [big_project(i) for i in range(10)], "size": 10, "meta": {}}
    srv = BatchServer({"/v1/projects/batch": (body, 10)})
    out, err = T.call_tool("get_project", {"ids": [f"prj_{i:020d}" for i in range(10)]},
                           api(srv))
    assert not err, out
    assert len(out["items"]) == 10
    assert T.estimate_tokens(T.to_text(out)) < T.TOKEN_BUDGET, T.estimate_tokens(T.to_text(out))
