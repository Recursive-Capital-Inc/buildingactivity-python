"""buildingactivity CLI: exit codes, pagination, dry run, 429 retry, commands from the
bundled spec, offline schema, property and changes tail. HTTP is a scripted fake session;
tests/test_spec.py covers the spec snapshot itself."""
import copy
import io
import json
import os
import stat

import pytest
import requests

from buildingactivity.cli import main as cli

KEY = "k-cli-secret-123"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def err_body(type_, message="nope", status=None):
    return {"error": {"type": type_, "message": message, "param": None,
                      "doc_url": "/docs/api/v1.md"}, "request_id": "req_" + "a" * 32}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv(cli.ENV_KEY, KEY)
    monkeypatch.setenv(cli.ENV_CONFIG, str(tmp_path / "config.json"))
    monkeypatch.delenv(cli.ENV_URL, raising=False)
    sleeps = []
    monkeypatch.setattr(cli, "_sleep", sleeps.append)
    state = {"sleeps": sleeps}

    def use(*responses):
        s = Session(*responses)
        monkeypatch.setattr(cli, "_session", lambda: s)
        state["session"] = s
        return s
    state["use"] = use
    return state


def run(*argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def page(items, nxt=None, key="items"):
    return Resp(200, {key: items, "size": len(items), "next_cursor": nxt})


# ---- exit codes ----------------------------------------------------------------------------

@pytest.mark.parametrize(("status", "type_", "code", "kind"), [
    (422, "validation_error", 1, "validation_error"),
    (400, "bad_request", 1, "client_error"),
    (404, "not_found", 1, "client_error"),
    (401, "auth_error", 2, "auth_error"),
    (402, "credit_exhausted", 4, "credit_exhausted"),
    (500, "server_error", 5, "server_error"),
    (503, "unavailable", 5, "server_error"),
])
def test_http_errors_map_to_exit_codes_with_json_on_stderr(env, status, type_, code, kind):
    env["use"](Resp(status, err_body(type_, "it broke")))
    rc, out, err = run("permits", "get", "pmt_aaaaaaaaaaaaaaaaaaaa")
    assert rc == code and out == ""
    body = json.loads(err)
    assert body["code"] == code and body["error_type"] == kind and body["status"] == status
    assert body["error"] == "it broke" and body["api_error_type"] == type_
    assert body["request_id"] == "req_" + "a" * 32
    assert KEY not in err


def test_ok_is_exit_0_with_the_body_on_stdout_and_the_key_in_the_header(env):
    s = env["use"](Resp(200, {"id": "pmt_x", "agency": "a"}))
    rc, out, err = run("permits", "get", "pmt_x", "--base-url", "http://h:1/")
    assert (rc, err) == (0, "") and json.loads(out) == {"id": "pmt_x", "agency": "a"}
    url, params, headers = s.calls[0]
    assert url == "http://h:1/v1/permits/pmt_x" and params == {}
    assert headers["X-API-Key"] == KEY and KEY not in out


def test_network_failure_is_exit_5(env):
    env["use"](requests.ConnectionError("refused"))
    rc, _out, err = run("usage")
    assert rc == 5 and json.loads(err)["error_type"] == "network_error"


def test_missing_key_is_exit_2_before_any_call(env, monkeypatch):
    monkeypatch.delenv(cli.ENV_KEY)
    s = env["use"]()
    rc, _out, err = run("permits", "search", "--geo-id", "CA")
    assert rc == 2 and json.loads(err)["error_type"] == "auth_error" and s.calls == []


def test_bad_flags_are_exit_1_not_argparse_2(env):
    s = env["use"]()
    for argv in (["permits", "search", "--nope"], ["permits", "search", "--status", "bogus"],
                 ["permits", "search", "--min-value", "abc"], ["geo", "states", "list",
                                                                  "--limit", "0"]):
        rc, _out, err = run(*argv)
        assert rc == 1 and json.loads(err)["error_type"] == "client_error", argv
    assert s.calls == []


# ---- pagination ------------------------------------------------------------------------------

def test_limit_all_follows_the_cursor_and_merges_pages(env):
    s = env["use"](page([1, 2], "c1"), page([3, 4], "c2"), page([5], None))
    rc, out, _err = run("permits", "search", "--geo-id", "cty_06041", "--limit", "all")
    body = json.loads(out)
    assert rc == 0 and body["items"] == [1, 2, 3, 4, 5] and body["size"] == 5
    assert body["next_cursor"] is None and body["cli"] == {"pages": 3, "records": 5}
    assert [c[1].get("cursor") for c in s.calls] == [None, "c1", "c2"]
    assert all(c[1]["geo_id"] == "cty_06041" and c[1]["limit"] == 200 for c in s.calls)


def test_limit_n_asks_only_for_what_is_left(env):
    s = env["use"](page([1, 2], "c1"), page([3], "c2"))
    rc, out, _err = run("permits", "search", "--limit", "3", "--page-size", "2")
    body = json.loads(out)
    assert rc == 0 and body["items"] == [1, 2, 3] and body["next_cursor"] == "c2"
    assert [c[1]["limit"] for c in s.calls] == [2, 1]


def test_max_records_caps_limit_all(env):
    s = env["use"](page([1, 2, 3], "c1"), page([4, 5, 6], "c2"))
    rc, out, _err = run("permits", "search", "--limit", "all", "--max-records", "4",
                        "--page-size", "3")
    assert rc == 0 and json.loads(out)["items"] == [1, 2, 3, 4]
    assert [c[1]["limit"] for c in s.calls] == [3, 1]


def test_without_limit_one_page_is_fetched(env):
    s = env["use"](page([1], "c1"))
    rc, out, _err = run("geo", "states", "list")
    assert rc == 0 and json.loads(out)["next_cursor"] == "c1" and len(s.calls) == 1
    assert "limit" not in s.calls[0][1]


def test_data_lists_paginate_too_and_one_result_commands_refuse_limit(env):
    env["use"](page([1], "2", key="data"), page([2], None, key="data"))
    rc, out, _err = run("geo", "counties", "list", "--state", "CA", "--limit", "all")
    assert rc == 0 and json.loads(out)["data"] == [1, 2]
    rc, _out, err = run("permits", "get", "pmt_x", "--limit", "5")
    assert rc == 1 and "list commands" in json.loads(err)["error"]


# ---- dry run ------------------------------------------------------------------------------

def test_dry_run_prints_the_url_and_makes_no_call_even_without_a_key(env, monkeypatch):
    monkeypatch.delenv(cli.ENV_KEY)
    s = env["use"]()
    rc, out, err = run("permits", "search", "--geo-id", "cty_06041", "--tags", "solar,-pool",
                       "--status", "issued", "--include-count", "--limit", "10", "--dry-run",
                       "--base-url", "http://127.0.0.1:8470")
    assert (rc, err, s.calls) == (0, "", [])
    assert out.strip() == ("http://127.0.0.1:8470/v1/permits/search?geo_id=cty_06041&"
                           "tags=solar&tags=-pool&include_count=true&status=issued&limit=10")
    rc, out, _err = run("addresses", "permits", "adr_x:u:2B", "--dry-run")
    assert out.strip() == cli.DEFAULT_URL + "/v1/addresses/adr_x:u:2B/permits"


# ---- retries ------------------------------------------------------------------------------

def test_429_retries_honoring_retry_after(env):
    s = env["use"](Resp(429, err_body("rate_limited"), {"Retry-After": "7"}),
                   Resp(429, err_body("rate_limited"), {"Retry-After": "2"}),
                   Resp(200, {"used": 1}))
    rc, out, _err = run("usage")
    assert rc == 0 and json.loads(out) == {"used": 1}
    assert env["sleeps"] == [7.0, 2.0] and len(s.calls) == 3


def test_429_after_all_retries_is_exit_3(env):
    env["use"](*[Resp(429, err_body("rate_limited"), {"Retry-After": "1"})] * 3)
    rc, _out, err = run("usage", "--retries", "2")
    body = json.loads(err)
    assert rc == 3 and body["error_type"] == "rate_limited"
    assert body["error"].startswith("rate limited after 2 retries")
    assert env["sleeps"] == [1.0, 1.0]


def test_no_retry_fails_at_once_and_retry_after_is_capped(env):
    env["use"](Resp(429, err_body("rate_limited"), {"Retry-After": "9"}))
    rc, _out, _err = run("usage", "--no-retry")
    assert rc == 3 and env["sleeps"] == []
    env["use"](Resp(429, err_body("rate_limited"), {"Retry-After": "99999"}), Resp(200, {}))
    assert run("usage")[0] == 0 and env["sleeps"] == [cli.MAX_RETRY_WAIT]


# ---- spec, schema, commands --------------------------------------------------------------

def test_every_v1_get_has_exactly_one_command():
    spec = cli.load_spec()
    ops = cli.operations(spec)
    keys = {f"GET {p}" for p, m in spec["paths"].items() if "get" in m}
    assert {o.key for o in ops.values()} == keys
    names = {o.command for o in ops.values()}
    assert {"permits search", "permits get", "permits batch", "properties lookup",
            "geo states list", "geo states get", "usage", "metrics"} <= names


def test_schema_is_offline(env):
    s = env["use"]()
    rc, out, _err = run("schema")
    cmds = {c["command"]: c for c in json.loads(out)}
    assert rc == 0 and cmds["permits search"]["paginated"] is True
    rc, out, _err = run("schema", "permits", "search")
    body = json.loads(out)
    assert rc == 0 and body["path"] == "/v1/permits/search"
    assert body["flags"]["geo_id"] == "--geo-id" and "401" in body["responses"]
    assert run("schema", "nope")[0] == 1 and s.calls == []


# ---- property ---------------------------------------------------------------------------

def test_property_three_lookups_and_by_id(env):
    s = env["use"](*[Resp(200, {"items": []})] * 4)
    assert run("property", "--apn", "05506128", "--county-fips", "06041")[0] == 0
    assert run("property", "--address", "50 Barbaree Way, Tiburon, CA 94920")[0] == 0
    assert run("property", "--lat", "37.89", "--lon", "-122.5", "--radius-m", "300")[0] == 0
    assert run("property", "prp_vuoc6a72qoqzzmubcrva")[0] == 0
    paths = [c[0].split("/v1", 1)[1] for c in s.calls]
    assert paths == ["/properties/lookup", "/properties/lookup", "/properties/near",
                     "/properties/prp_vuoc6a72qoqzzmubcrva"]
    assert s.calls[0][1] == {"apn": "05506128", "county_fips": "06041"}
    assert s.calls[2][1] == {"lat": 37.89, "lon": -122.5, "radius_m": 300.0}


def test_property_needs_county_fips_with_apn_and_some_input(env):
    s = env["use"]()
    assert run("property", "--apn", "1")[0] == 1
    assert run("property")[0] == 1
    assert s.calls == []


# ---- changes tail --------------------------------------------------------------------------

def test_changes_tail_without_a_feed_says_so(env, monkeypatch):
    spec = copy.deepcopy(cli.load_spec())
    spec["paths"].pop("/v1/changes", None)
    monkeypatch.setattr(cli, "load_spec", lambda: spec)
    rc, _out, err = run("changes", "tail")
    body = json.loads(err)
    assert rc == 1 and body["error_type"] == "unsupported" and "/v1/changes" in body["error"]


def test_changes_tail_follows_the_feed(env, monkeypatch):
    spec = copy.deepcopy(cli.load_spec())
    spec["paths"]["/v1/changes"] = {"get": {"summary": "Changes", "responses": {}, "parameters": [
        {"name": "since", "in": "query", "schema": {"type": "string"}},
        {"name": "cursor", "in": "query", "schema": {"type": "string"}}]}}
    monkeypatch.setattr(cli, "load_spec", lambda: spec)
    s = env["use"](page([{"n": 1}, {"n": 2}], "c1"), page([], None),
                   page([{"n": 3}], "c2"), page([], None))
    rc, out, _err = run("changes", "tail", "--since", "2026-10-19", "--interval", "5",
                        "--max-records", "3")
    assert rc == 0 and [json.loads(x)["n"] for x in out.splitlines()] == [1, 2, 3]
    assert env["sleeps"] == [5.0]
    assert [c[1].get("cursor") for c in s.calls] == [None, "c1", "c1"]
    assert all(c[1]["since"] == "2026-10-19" for c in s.calls)
    s = env["use"](page([{"n": 4}], None))
    assert run("changes", "tail", "--once")[0] == 0 and len(s.calls) == 1
    assert run("changes", "list", "--dry-run")[1].strip().endswith("/v1/changes")


def test_changes_tail_keeps_paging_through_an_empty_page_with_has_more(env):
    # the bundled spec has the real /v1/changes: since and cursor parameters
    s = env["use"](Resp(200, {"items": [], "size": 0, "next_cursor": "10", "has_more": True}),
                   Resp(200, {"items": [{"n": 1}], "size": 1, "next_cursor": "20",
                              "has_more": False}))
    rc, out, _err = run("changes", "tail", "--once")
    assert rc == 0 and [json.loads(x)["n"] for x in out.splitlines()] == [1]
    assert [c[1].get("cursor") for c in s.calls] == [None, "10"]
    assert all("include_backfill" not in c[1] for c in s.calls)
    # the backfill opt-in reaches the feed (it is off unless asked for)
    rc, out, _err = run("changes", "tail", "--include-backfill", "--once", "--dry-run")
    assert rc == 0 and out.strip().endswith("/v1/changes?include_backfill=true")


# ---- config -----------------------------------------------------------------------------

def test_config_set_key_from_stdin_is_private_and_never_shown(env, monkeypatch):
    monkeypatch.delenv(cli.ENV_KEY)
    monkeypatch.setattr("sys.stdin", io.StringIO("k-from-stdin\n"))
    rc, out, _err = run("config", "set-key")
    assert rc == 0 and "k-from-stdin" not in out
    path = cli.config_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert cli.api_key() == ("k-from-stdin", "config")
    rc, out, _err = run("config", "show")
    assert "k-from-stdin" not in out and json.loads(out)["api_key"] == "set (from config file)"
    assert run("config", "set-url", "http://x:9/")[0] == 0 and cli.base_url() == "http://x:9"


# ---- end to end against the app ------------------------------------------------------------


def test_default_api_root_is_the_public_api(env, monkeypatch):
    monkeypatch.delenv(cli.ENV_URL, raising=False)
    assert cli.base_url() == "https://api.buildingactivity.com"
    rc, out, _err = run("usage", "--dry-run")
    assert rc == 0 and out.strip() == "https://api.buildingactivity.com/v1/usage"
