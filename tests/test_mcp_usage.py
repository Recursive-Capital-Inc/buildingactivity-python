"""Usage events of the hosted MCP server (src/buildingactivity/mcp/usage.py, docs/hosted.md).

Hermetic: the fixture API session from test_mcp_tools answers every GET. The key is a fake.
The privacy tests read every byte the server logged (all loggers, DEBUG) and every byte it
wrote to the usage directory, and require that neither the key nor an argument value is in
there, whichever way the key was sent.
"""
import datetime as dt
import hashlib
import json
import logging

import pytest

from buildingactivity.mcp import tools as T
from buildingactivity.mcp import usage as U
from test_mcp_tools import KEY, FixtureServer

pytest.importorskip("mcp")
pytest.importorskip("httpx")
from starlette.testclient import TestClient

from buildingactivity.mcp import server

REF = hashlib.sha256(KEY.encode()).hexdigest()[:16]
UA = "usage-test-agent/1.0"
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
       "MCP-Protocol-Version": "2025-06-18", "User-Agent": UA}
ADDRESS = "SENTINEL-ADDR 4417 Quillfeather Lane"
CONTRACTOR = "SENTINEL-CTR Zyxwv Roofing"


def init(name="claude-ai", version="0.9.1", rid=1, pad=None):
    info = {"name": name, "version": version}
    if pad:
        info["padding"] = pad
    return {"jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": info}}


def call(name, arguments, rid=2):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture
def sink(tmp_path):
    return U.UsageSink(directory=str(tmp_path / "usage"), keep_days=400)


@pytest.fixture(autouse=True)
def _usage_dir(tmp_path, monkeypatch):
    (tmp_path / "usage").mkdir()
    monkeypatch.delenv(U.ENV_DIR, raising=False)
    monkeypatch.delenv(U.ENV_KEEP_DAYS, raising=False)


def client(sink, srv=None):
    app = server.http_app(T.Api("http://fixture", None, srv or FixtureServer()), usage=sink)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def events(caplog, kind=None):
    out = [json.loads(r.getMessage()) for r in caplog.records
           if r.name == "buildingactivity.mcp.usage" and r.levelno == logging.INFO]
    return [e for e in out if kind is None or e["event"] == kind]


def file_text(tmp_path):
    return "".join(p.read_text() for p in sorted((tmp_path / "usage").glob("*")))


def file_events(tmp_path):
    return [json.loads(line) for line in file_text(tmp_path).splitlines()]


SENDS = {  # key_via -> (url, extra headers)
    "header": ("/mcp", {"X-API-Key": KEY}),
    "bearer": ("/mcp", {"Authorization": f"Bearer {KEY}"}),
    "path": (f"/mcp/{KEY}", {}),
}


@pytest.mark.parametrize("via", list(SENDS))
def test_each_key_form_writes_one_tool_call_with_its_ref_and_via(via, sink, logs, tmp_path):
    url, extra = SENDS[via]
    with client(sink) as c:
        r = c.post(url, json=call("geo_lookup", {"q": "Marin"}), headers={**HDR, **extra})
        assert r.status_code == 200, r.text
        assert not r.json()["result"].get("isError")
    got = events(logs, "tool_call")
    assert len(got) == 1
    e = got[0]
    assert (e["key_ref"], e["key_via"], e["tool"], e["outcome"]) == (REF, via, "geo_lookup", "ok")
    assert e["status"] is None and e["records_charged"] == 2  # fixture geo rows
    assert isinstance(e["latency_ms"], (int, float)) and e["latency_ms"] >= 0
    assert e["args"] == {"q": 5}
    assert e["user_agent"] == UA and e["protocol_version"] == "2025-06-18"
    assert e["server_version"] == server._version()
    assert e["ts"].endswith("Z") and len(e["ts"]) == len("2026-10-28T20:41:07.123Z")
    assert file_events(tmp_path) == got


@pytest.mark.parametrize("via", list(SENDS))
def test_key_and_argument_values_never_reach_logs_or_files(via, sink, logs, tmp_path, capfd):
    url, extra = SENDS[via]
    with client(sink) as c:
        h = {**HDR, **extra}
        assert c.post(url, json=init(), headers=h).status_code == 200
        r = c.post(url, json=call("resolve_address", {"address": ADDRESS, "state": "CA"}),
                   headers=h)
        assert r.status_code == 200, r.text
        r = c.post(url, json=call("search_permits", {"contractor": CONTRACTOR, "limit": 5,
                                                     "tags": ["roofing", "solar"]}), headers=h)
        assert r.status_code == 200, r.text
    out, err = capfd.readouterr()
    # Everything the server side logged. The test client's own httpx request log names the
    # URL it called (the key, for /mcp/<key>); that logger is client-side and not in the
    # server, whose HTTP access log is off (main(), uvicorn access_log=False).
    server_records = [r for r in logs.records if not r.name.startswith("httpx")]
    assert len(server_records) > 3
    written = "\n".join([*(f"{r.name} {r.getMessage()}" for r in server_records),
                         *(ln for ln in (out + err).splitlines() if "httpx" not in ln),
                         file_text(tmp_path)])
    for secret in (KEY, KEY[:8], KEY[-8:], ADDRESS, CONTRACTOR, "Quillfeather", "Zyxwv",
                   "roofing", "solar"):
        assert secret not in written, secret
    calls = [e for e in file_events(tmp_path) if e["event"] == "tool_call"]
    assert [e["args"] for e in calls] == [
        {"address": len(ADDRESS), "state": 2},
        {"contractor": len(CONTRACTOR), "limit": None, "tags": 2}]
    assert {e["key_ref"] for e in file_events(tmp_path)} == {REF}


def test_unknown_argument_names_and_tools_are_not_written(sink, logs, tmp_path):
    with client(sink) as c:
        h = {**HDR, "X-API-Key": KEY}
        c.post("/mcp", json=call("geo_lookup", {"q": "x", ADDRESS: "y"}), headers=h)
        c.post("/mcp", json=call(CONTRACTOR, {"q": "x"}), headers=h)
    assert ADDRESS not in file_text(tmp_path) and CONTRACTOR not in file_text(tmp_path)
    a, b = events(logs, "tool_call")
    assert a["args"] == {"q": 1, "_other": 1}
    assert (b["tool"], b["outcome"], b["args"]) == ("unknown", "not_found", {"_other": 1})


def test_api_error_records_type_status_and_latency(sink, logs):
    srv = FixtureServer(status=429, body={"error": {"type": "rate_limited", "message": "slow",
                                                    "param": None}})
    with client(sink, srv) as c:
        r = c.post("/mcp", json=call("my_usage", {}), headers={**HDR, "X-API-Key": KEY})
        assert r.json()["result"]["isError"] is True
    (e,) = events(logs, "tool_call")
    assert (e["outcome"], e["status"], e["records_charged"]) == ("rate_limited", 429, 0)
    assert isinstance(e["latency_ms"], (int, float)) and e["latency_ms"] >= 0


def test_initialize_client_info_joins_later_calls_by_key_and_user_agent(sink, logs, tmp_path):
    with client(sink) as c:
        h = {**HDR, "X-API-Key": KEY}
        assert c.post("/mcp", json=init("claude-ai", "0.9.1"), headers=h).status_code == 200
        assert c.post("/mcp", json=call("my_usage", {}), headers=h).status_code == 200
        other = {**h, "User-Agent": "someone-else/2"}
        assert c.post("/mcp", json=call("my_usage", {}), headers=other).status_code == 200
        bearer = {**HDR, "Authorization": f"Bearer {KEY}"}  # same key, same UA, other form
        assert c.post("/mcp", json=call("my_usage", {}), headers=bearer).status_code == 200
    (i,) = events(logs, "initialize")
    assert (i["key_ref"], i["key_via"], i["client_name"], i["client_version"],
            i["protocol_version"], i["user_agent"]) == (REF, "header", "claude-ai", "0.9.1",
                                                        "2025-06-18", UA)
    same, diff, bearer_call = events(logs, "tool_call")
    assert (same["client_name"], same["client_version"]) == ("claude-ai", "0.9.1")
    assert (diff["client_name"], diff["client_version"]) == (None, None)
    assert (bearer_call["client_name"], bearer_call["key_via"]) == ("claude-ai", "bearer")
    assert [e["event"] for e in file_events(tmp_path)] == ["initialize"] + ["tool_call"] * 3


def test_initialize_in_a_batch_and_client_strings_are_cleaned(sink, logs):
    with client(sink) as c:
        body = [init("evil\x1b[31mname\n" + "n" * 100, "1\x00.2", rid=7)]
        c.post(f"/mcp/{KEY}", json=body, headers=HDR)
    (i,) = events(logs, "initialize")
    assert i["key_via"] == "path"
    assert i["client_name"] == ("evil[31mname" + "n" * 100)[:64]
    assert i["client_version"] == "1.2"


def test_a_client_cannot_claim_key_via_path(sink, logs):
    with client(sink) as c:
        h = {**HDR, "X-API-Key": KEY, server.KEY_VIA_HEADER: "path"}
        assert c.post("/mcp", json=call("my_usage", {}), headers=h).status_code == 200
    assert events(logs, "tool_call")[0]["key_via"] == "header"


def test_initialize_body_over_64kb_passes_through_unparsed(sink, logs):
    with client(sink) as c:
        h = {**HDR, "X-API-Key": KEY}
        big = init(pad="x" * (server.INIT_BODY_CAP + 10))
        r = c.post("/mcp", json=big, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == server.NAME
        r = c.post("/mcp", json=call("my_usage", {}), headers=h)
        assert r.status_code == 200 and not r.json()["result"].get("isError")
    assert events(logs, "initialize") == []
    (e,) = events(logs, "tool_call")
    assert e["client_name"] is None and e["outcome"] == "ok"


def test_old_files_are_pruned_newer_kept(tmp_path):
    d = tmp_path / "usage"
    for name in ("usage-2025-01-01.jsonl", "usage-2025-09-22.jsonl", "usage-2025-09-23.jsonl",
                 "usage-2026-10-27.jsonl", "notes.txt"):
        (d / name).write_text("{}\n")
    now = dt.datetime(2026, 10, 28, 12, tzinfo=dt.UTC)
    U.UsageSink(directory=str(d), keep_days=400, now=lambda: now)
    assert sorted(p.name for p in d.iterdir()) == [
        "notes.txt", "usage-2025-09-23.jsonl", "usage-2026-10-27.jsonl"]


def test_pruning_runs_again_when_the_utc_day_changes(tmp_path):
    d = tmp_path / "usage"
    clock = [dt.datetime(2026, 10, 28, 23, 59, tzinfo=dt.UTC)]
    s = U.UsageSink(directory=str(d), keep_days=2, now=lambda: clock[0])
    s.emit({"event": "x"})
    (d / "usage-2026-10-27.jsonl").write_text("{}\n")
    clock[0] = dt.datetime(2026, 10, 29, 0, 1, tzinfo=dt.UTC)
    s.emit({"event": "y"})
    assert (d / "usage-2026-10-27.jsonl").exists()  # cutoff on 10-29 is 10-27: kept
    clock[0] = dt.datetime(2026, 10, 30, 0, 1, tzinfo=dt.UTC)
    s.emit({"event": "z"})
    assert not (d / "usage-2026-10-27.jsonl").exists()  # cutoff 10-28: pruned
    assert sorted(p.name for p in d.iterdir()) == [
        "usage-2026-10-28.jsonl", "usage-2026-10-29.jsonl", "usage-2026-10-30.jsonl"]


def test_no_file_without_the_env_var(tmp_path, monkeypatch, logs):
    monkeypatch.chdir(tmp_path)
    app = server.http_app(T.Api("http://fixture", None, FixtureServer()))
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.post("/mcp", json=call("my_usage", {}), headers={**HDR, "X-API-Key": KEY})
        assert r.status_code == 200
    assert len(events(logs, "tool_call")) == 1  # stderr only
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["usage"]
    assert list((tmp_path / "usage").iterdir()) == []


def test_env_var_sets_the_dir_and_keep_days(tmp_path, monkeypatch):
    monkeypatch.setenv(U.ENV_DIR, str(tmp_path / "usage"))
    monkeypatch.setenv(U.ENV_KEEP_DAYS, "30")
    s = U.UsageSink.from_env()
    assert (s.directory, s.keep_days) == (str(tmp_path / "usage"), 30)
    app = server.http_app(T.Api("http://fixture", None, FixtureServer()))
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.post("/mcp", json=call("my_usage", {}), headers={**HDR, "X-API-Key": KEY})
    assert [e["event"] for e in file_events(tmp_path)] == ["tool_call"]


def test_unwritable_dir_warns_once_and_tool_calls_still_work(tmp_path, logs):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    bad = U.UsageSink(directory=str(blocker / "usage"))
    with client(bad) as c:
        for _ in range(3):
            r = c.post("/mcp", json=call("my_usage", {}), headers={**HDR, "X-API-Key": KEY})
            assert r.status_code == 200 and not r.json()["result"].get("isError")
    warnings = [r for r in logs.records
                if r.name == "buildingactivity.mcp.usage" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert len(events(logs, "tool_call")) == 3


def test_a_failing_sink_never_fails_the_tool_call(logs):
    class Broken(U.UsageSink):
        def tool_call(self, **kw):
            raise RuntimeError("boom")

    with client(Broken()) as c:
        r = c.post("/mcp", json=call("my_usage", {}), headers={**HDR, "X-API-Key": KEY})
        assert r.status_code == 200 and not r.json()["result"].get("isError")
    assert any("usage event dropped" in r.getMessage() for r in logs.records)


def test_key_ref_and_arg_shape_units():
    assert U.key_ref(KEY) == REF and len(REF) == 16 and U.key_ref("") is None
    assert U.arg_shape({"a": "xyz", "b": [1, 2], "c": {"k": 1}, "d": 5, "e": True,
                        "f": None}) == {"a": 3, "b": 2, "c": 1, "d": None, "e": None,
                                        "f": None}


def test_client_cache_is_bounded_and_expires():
    t = [0.0]
    cache = U.ClientCache(size=2, ttl=10, clock=lambda: t[0])
    cache.put("r1", "ua", "a", "1")
    cache.put("r2", "ua", "b", "1")
    cache.put("r3", "ua", "c", "1")
    assert cache.get("r1", "ua") == (None, None) and cache.get("r3", "ua") == ("c", "1")
    t[0] = 11
    assert cache.get("r3", "ua") == (None, None)


def test_streamable_http_info_logger_is_quieted(monkeypatch):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    logger = logging.getLogger("mcp.server.streamable_http")
    monkeypatch.setattr(logger, "level", logger.level)
    server.main(["--transport", "http"])
    assert logger.level == logging.WARNING and seen["access_log"] is False
