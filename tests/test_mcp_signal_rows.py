"""search_signals rows carry description, location and APNs, while get_signal keeps the
long text and more APNs. Serves recorded-style bodies, no network."""

from buildingactivity.mcp import tools as T

KEY = "k-mcp-secret-123"

RICH = {"id": "sig_a", "title": "A signal", "category": "ceqa_nod", "status": "filed",
        "description": "x" * 300, "location": "123 Main St, San Rafael, CA 94901",
        "apns": ["061-2", "061-1", "061-2", "061-3", "061-4", "061-5"],
        "applicant": "ACME", "case_numbers": ["CN-1"], "lat": 37.9, "lon": -122.5,
        "source_url": "https://example.com/a"}
BARE = {"id": "sig_b", "title": "B signal", "status": "filed",
        "source_url": "https://example.com/b"}
PAGE = {"items": [dict(RICH), dict(BARE)], "next_cursor": None}
LONG_DESC = "y" * 500


class SignalServer:
    """requests-style session answering /v1/signals* with the given bodies."""

    def __init__(self, page_body=PAGE, signal_body=None):
        self.page_body, self.signal_body = page_body, signal_body
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        body = self.page_body if path == "/v1/signals" else self.signal_body
        self.calls.append((path, dict(params or {})))
        return Resp(200, body)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {"X-Records-Request": "1", "X-Request-Id": "req_" + "a" * 32}

    def json(self):
        return self._body


def api(server=None):
    return T.Api("http://fixture", KEY, server or SignalServer())


def test_search_rows_carry_description_location_and_apns():
    payload, err = T.call_tool("search_signals", {"geo_id": "cty_06037"}, api())
    assert not err
    rich, bare = payload["items"]
    assert rich["description"].startswith("xxx") and len(rich["description"]) <= 160
    assert rich["description"].endswith("...")
    assert rich["location"] == RICH["location"]
    assert rich["apns"] == ["061-1", "061-2", "061-3"]
    for key in ("description", "location", "apns"):
        assert key not in bare


def test_get_signal_keeps_long_description_and_five_apns():
    body = dict(RICH, id="sig_a", description=LONG_DESC, title="A signal")
    payload, err = T.call_tool("get_signal", {"id": "sig_a"}, api(SignalServer(signal_body=body)))
    assert not err
    item = payload["item"]
    assert item["description"] == LONG_DESC
    assert len(item["apns"]) == 5


def test_tool_description_mentions_location_apns_description_and_fits():
    desc = T.TOOLS["search_signals"].description
    assert all(w in desc for w in ("location", "APNs", "description"))
    assert len(desc) <= 200


def test_request_params_are_unchanged():
    srv = SignalServer()
    T.call_tool("search_signals", {"geo_id": "cty_06037", "category": ["ceqa_nod"],
                                   "limit": 3, "cursor": "c1"}, api(srv))
    assert srv.calls[0] == ("/v1/signals",
                            {"geo_id": "cty_06037", "category": "ceqa_nod", "limit": 3,
                             "cursor": "c1"})
