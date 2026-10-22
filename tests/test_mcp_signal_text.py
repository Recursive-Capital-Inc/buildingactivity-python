"""get_signal returns the signal's title and description unclipped (up to DETAIL_TEXT_MAX),
while search_signals rows stay compact. Serves the recorded /v1/signals/<id> bodies from
tests/fixtures/signal_text/api_signal_bodies.json, no network.
"""
import json
import pathlib

from buildingactivity.mcp import tools as T

KEY = "k-mcp-secret-123"
BODIES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "signal_text"
                     / "api_signal_bodies.json").read_text())


def collapse(text):
    return " ".join((text or "").split())


class SignalServer:
    """requests-style session answering /v1/signals* from the recorded bodies."""

    def __init__(self, page_body=None, signal_body=None):
        self.page_body = page_body
        self.signal_body = signal_body

    def get(self, url, params=None, headers=None, timeout=None):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        if path == "/v1/signals":
            body = self.page_body
        else:
            sig_id = path.rsplit("/", 1)[-1]
            body = self.signal_body or BODIES[sig_id]
        return Resp(200, body)


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {"X-Records-Request": "1",
                        "X-Request-Id": "req_" + "a" * 32}

    def json(self):
        return self._body


def api(server=None):
    return T.Api("http://fixture", KEY, server or SignalServer())


def test_get_signal_description_unclipped():
    out = T.call_tool("get_signal", {"id": "sig_zjrv3ty4pxn3zvarvjqa"}, api())
    payload, err = out
    assert not err
    desc = payload["item"]["description"]
    assert desc == collapse(BODIES["sig_zjrv3ty4pxn3zvarvjqa"]["description"])
    assert len(desc) > 600
    assert not desc.endswith("...")


def test_get_signal_title_unclipped():
    payload, err = T.call_tool("get_signal", {"id": "sig_uvr6vjt5uwdppyywcq3q"}, api())
    assert not err
    title = payload["item"]["title"]
    assert title == collapse(BODIES["sig_uvr6vjt5uwdppyywcq3q"]["title"])
    assert len(title) > 120
    assert not title.endswith("...")


def test_get_signal_keeps_address_and_omits_empty_description():
    payload, _ = T.call_tool("get_signal", {"id": "sig_6734mymxfsqquvoggeca"}, api())
    item = payload["item"]
    assert "13131 N. U.S. 169 Highway" in item["title"]
    assert "description" not in item


def test_get_signal_description_clipped_to_detail_max():
    body = dict(BODIES["sig_zjrv3ty4pxn3zvarvjqa"])
    body["description"] = "x" * 5000
    payload, _ = T.call_tool("get_signal", {"id": "sig_zjrv3ty4pxn3zvarvjqa"},
                             api(SignalServer(signal_body=body)))
    desc = payload["item"]["description"]
    assert len(desc) == 2000
    assert desc.endswith("...")


def test_search_signals_title_still_clipped_to_120():
    page = {"items": [dict(b) for b in BODIES.values()], "next_cursor": None}
    payload, _ = T.call_tool("search_signals", {"geo_id": "cty_06037"},
                             api(SignalServer(page_body=page)))
    for row in payload["items"]:
        assert len(row["title"]) <= 120
