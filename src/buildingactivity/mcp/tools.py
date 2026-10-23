"""MCP tool layer: a thin client of the /v1 HTTP API with compact, budgeted answers.

Every tool makes one GET to /v1 (recent_changes with a webhook_id makes two: the saved
filter, then the feed), so it inherits the API's auth, metering and error shape; nothing
here reads a database. A tool answer is one JSON object:

    {"items": [...compact rows...], "next_cursor": "..." | null, "records_charged": 10}

or {"item": {...}} for one-record tools. `records_charged` is the API's
`X-Records-Request` header for that call. Rows keep a few selected fields; record rows
(permits, decisions, projects, signals) always carry `source_url`, null only when the
issuer gave none. Long text is clipped so a default page stays under TOKEN_BUDGET
(chars / 4). An API error comes back as {"error": {"type", "message", "param"},
"status", "request_id", "records_charged": 0} with is_error set.

This module has no MCP SDK dependency: server.py adapts it to the SDK's transports, and
tests drive call_tool() directly.
"""
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

ENV_URL = "BUILDINGACTIVITY_API_URL"
ENV_KEY = "BUILDINGACTIVITY_API_KEY"
DEFAULT_URL = "https://api.buildingactivity.com"
TOKEN_BUDGET = 4000  # per default-size answer, estimated as len(json) / 4
TEXT_MAX = 160  # characters kept of a description or title
DETAIL_TEXT_MAX = 2000  # characters kept of a detail answer's title or description
FIELD_MAX = 80  # characters kept of any other text field (IDs and URLs are kept whole)
SIGNAL_ROW_TEXT_MAX = 100  # search_signals descriptions only: 160 chars overflow the budget
METRIC_ROWS = 36  # rows of one metrics answer
TIMEOUT = 30.0

PERMIT_SORTS = ("value_desc", "value_asc", "applied_desc", "applied_asc", "issued_desc",
                "issued_asc", "updated_desc", "updated_asc", "first_seen_desc",
                "first_seen_asc", "last_seen_desc")
PERMIT_STATUSES = ("applied", "in_review", "approved", "issued", "inspection", "finaled",
                   "expired", "withdrawn", "denied", "on_hold", "closed", "unknown")
PERMIT_CLASSES = ("all", "admin", "building", "business_license", "code_enforcement",
                  "electrical", "engineering_row", "environmental_health", "fire",
                  "legacy_unknown", "mechanical", "other", "planning", "plumbing")
GEO_LEVELS = ("state", "metro", "county", "jurisdiction", "zip")
METRIC_DIMS = ("month", "tag", "class", "status", "jurisdiction")
CONTRACTOR_SORTS = ("permits_desc", "value_desc", "last_permit_desc")
PROJECT_SORTS = ("newest", "value_desc")
SIGNAL_SOURCES = ("ceqanet", "legistar", "planning_case")
CHANGE_TYPES = ("created", "changed")
CHANGE_FIELDS = ("status", "status_detail", "record_date", "permit_type", "description",
                 "address", "source_url", "value", "contractor", "date_applied", "date_issued",
                 "date_finaled", "date_expires")


# ---- HTTP ------------------------------------------------------------------------------

@dataclass
class Reply:
    status: int
    body: object
    records: int | None
    request_id: str | None


class Api:
    """GETs against one API base URL with one key. `session` is anything with a
    requests-style `get(url, params=, headers=, timeout=)` (requests.Session, a
    starlette TestClient). The key is sent as X-API-Key and never logged or returned."""

    def __init__(self, base_url=None, key=None, session=None, timeout=TIMEOUT):
        self.base_url = (base_url or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")
        self._key = key if key is not None else os.environ.get(ENV_KEY)
        self._session = session
        self.timeout = timeout

    def __repr__(self):
        return f"Api(base_url={self.base_url!r}, key={'set' if self._key else 'unset'})"

    @property
    def has_key(self):
        return bool(self._key)

    def with_key(self, key):
        return Api(self.base_url, key, self._session, self.timeout)

    def _http(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def get(self, path, params=None):
        query = {k: _wire(v) for k, v in (params or {}).items() if v is not None and v != []}
        try:
            resp = self._http().get(self.base_url + path, params=query,
                                    headers={"X-API-Key": self._key or "",
                                             "Accept": "application/json"},
                                    timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - any transport failure is one error type
            raise ToolError("unavailable", f"cannot reach the API at {self.base_url}: "
                            f"{type(exc).__name__}") from None
        headers = resp.headers or {}
        try:
            body = resp.json()
        except ValueError:
            body = None
        rec = headers.get("X-Records-Request")
        return Reply(resp.status_code, body, int(rec) if rec and rec.isdigit() else None,
                     headers.get("X-Request-Id"))


def _wire(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list | tuple):
        return ",".join(str(x) for x in v)
    return v


class ToolError(Exception):
    def __init__(self, type_, message, param=None, status=None, request_id=None):
        super().__init__(message)
        self.type, self.message, self.param = type_, message, param
        self.status, self.request_id = status, request_id

    def payload(self):
        out: dict = {"error": {"type": self.type, "message": self.message,
                               "param": self.param}}
        if self.status is not None:
            out["status"] = self.status
        if self.request_id:
            out["request_id"] = self.request_id
        out["records_charged"] = 0
        return out


def _fetch(api, path, params=None):
    if not api.has_key:
        raise ToolError("auth_error", f"no API key: set {ENV_KEY} (or send X-API-Key to the "
                        "HTTP transport)")
    r = api.get(path, params)
    if r.status >= 400 or not isinstance(r.body, dict):
        err = (r.body or {}).get("error") if isinstance(r.body, dict) else None
        if isinstance(err, dict):
            raise ToolError(err.get("type") or "server_error", err.get("message") or "",
                            err.get("param"), r.status, r.request_id)
        raise ToolError("server_error", f"HTTP {r.status} without a v1 error body",
                        status=r.status, request_id=r.request_id)
    return r


def _seg(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ToolError("validation_error", f"{name} is required", name)
    return quote(value.strip(), safe=":")


# ---- compact rows ----------------------------------------------------------------------

def _clip(text, n=TEXT_MAX):
    if not isinstance(text, str):
        return text
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _whole(key):
    return key == "id" or key.endswith(("_id", "_url", "_ids"))


LONG_TEXT = ("description", "title", "name")


def _fit(key, v):
    if isinstance(v, str):
        return v if _whole(key) else _clip(v, TEXT_MAX if key in LONG_TEXT else FIELD_MAX)
    if isinstance(v, list):
        return [_fit(key, x) for x in v]
    return v


def _prune(row, keep=("source_url",)):
    """Drop null and empty fields, except those in `keep`, and clip text fields to
    FIELD_MAX characters (descriptions and titles arrive clipped to TEXT_MAX)."""
    return {k: _fit(k, v) for k, v in row.items()
            if k in keep or v not in (None, "", [], {})}


def permit_row(p):
    link = p.get("contractor_link") or {}
    return _prune({
        "id": p.get("id"),
        "permit_number": p.get("permit_number") or p.get("record_id"),
        "agency": p.get("agency"),
        "agency_name": p.get("agency_name"),
        "address": p.get("address"),
        "status": p.get("status_code") or p.get("status"),
        "type": p.get("permit_type"),
        "class": p.get("permit_class"),
        "description": _clip(p.get("description") or p.get("project_name")),
        "value_usd": None if p.get("value_suspect") else p.get("value_usd"),
        "applied": p.get("date_applied"),
        "issued": p.get("date_issued"),
        "finaled": p.get("date_finaled"),
        "contractor": p.get("contractor_name"),
        "contractor_id": link.get("contractor_id"),
        "source_url": p.get("source_url"),
        "source_url_kind": p.get("source_url_kind"),  # record page, or the issuer's portal
    })


def address_row(a):
    return _prune({"id": a.get("id"), "formatted": a.get("formatted"),
                   "permit_count": a.get("permit_count"), "match": a.get("match"),
                   "county_fips": a.get("county_fips"),
                   "jurisdiction_id": a.get("jurisdiction_id")}, keep=())


def property_row(p):
    addr = p.get("address") or {}
    return _prune({
        "id": p.get("id"), "kind": p.get("kind"), "address_id": p.get("address_id"),
        "address": addr.get("formatted") if isinstance(addr, dict) else addr,
        "parcel_ids": (p.get("parcel_ids") or [])[:5], "permit_count": p.get("permit_count"),
        "total_value_usd": p.get("total_value_usd"),
        "last_permit_date": p.get("last_permit_date"), "agencies": p.get("agencies"),
        "tags": (p.get("tags") or [])[:10],
        "contractors": [c.get("name") for c in (p.get("contractors") or [])[:5]
                        if isinstance(c, dict)],
        "distance_m": p.get("distance_m"),
    }, keep=())


def contractor_row(c):
    lic = c.get("license") or {}
    health = c.get("license_health") or {}
    scoped = c.get("scoped") or {}
    return _prune({
        "id": c.get("id"), "name": c.get("name"), "kind": c.get("kind"),
        "state": c.get("state"),
        "license": lic.get("number"), "license_status": health.get("status_class")
        or lic.get("status_class"),
        "license_source": lic.get("source"),
        "classifications": (c.get("classification_derived") or [])[:5],
        "phone": c.get("phone"), "permit_count": c.get("permit_count"),
        "total_job_value": c.get("total_job_value"),
        "last_permit_date": c.get("last_permit_date"),
        "scoped_permits": scoped.get("permits"),
    }, keep=())


def decision_row(d):
    return _prune({
        "id": d.get("id"), "title": _clip(d.get("title")), "category": d.get("category"),
        "subcategory": d.get("subcategory"), "status": d.get("status"),
        "decision_date": d.get("decision_date"), "body": d.get("body"),
        "place": d.get("place_name"), "state": d.get("state"),
        "address_id": d.get("address_id"), "case_numbers": (d.get("case_numbers") or [])[:3],
        "source": d.get("source"), "source_url": d.get("source_url"),
    })


def project_row(p):
    stage = p.get("stage") or {}
    urls = p.get("source_urls") or []
    return _prune({
        "id": p.get("id"), "name": _clip(p.get("name"), 120), "state": p.get("state"),
        "city": p.get("city"), "address": p.get("address"), "value_usd": p.get("value_usd"),
        "stage": stage.get("stage"), "stage_date": stage.get("date"),
        "permit_count": p.get("permit_count"), "signal_count": p.get("signal_count"),
        "latest_item_date": p.get("latest_item_date"),
        "source_url": urls[0] if urls else None,
    })


def project_item_row(i):
    return _prune({"id": i.get("id"), "kind": i.get("kind"), "title": _clip(i.get("title"), 100),
                   "date": i.get("date"), "status": i.get("status"),
                   "source_url": i.get("source_url")})


SIZE_FIELDS = ("units", "sqft", "acres", "mw", "stated_value_usd")


def signal_row(s):
    """An early signal: stage is the signal's status (filed, approved, ...); size is what
    its text states (units, sqft, acres, MW, dollars), with `large` and its basis."""
    size = {k: s.get(k) for k in SIZE_FIELDS if s.get(k) is not None}
    return _prune({
        "id": s.get("id"), "title": _clip(s.get("title"), 120), "category": s.get("category"),
        "description": _clip(s.get("description"), SIGNAL_ROW_TEXT_MAX), "location": s.get("location"),
        "apns": sorted(set(s.get("apns") or []))[:3],
        "doc_type": s.get("doc_type"), "stage": s.get("status"),
        "filed": s.get("filed_date"), "event_date": s.get("event_date"),
        "agency": s.get("agency"), "jurisdiction": s.get("jurisdiction"),
        "state": s.get("state"), "size": size, "large": s.get("large") or None,
        "size_basis": (s.get("size_basis") or [])[:3], "project_id": s.get("project_id"),
        "source_url": s.get("url") or s.get("source_url"),
    })


def change_row(e):
    p = e.get("permit") or {}
    row = {"seq": e.get("seq"), "type": (e.get("type") or "").removeprefix("permit."),
           "at": e.get("observed_at"), "permit_id": e.get("permit_id"),
           "backfill": e.get("backfill") or None, "field": e.get("field"),
           "old": _clip(e.get("old"), 60), "new": _clip(e.get("new"), 60)}
    if p:
        row.update({"permit_number": p.get("permit_number") or p.get("record_id"),
                    "agency": p.get("agency"), "agency_name": p.get("agency_name"),
                    "address": p.get("address"),
                    "city": p.get("city"), "status": p.get("status_code") or p.get("status"),
                    "class": p.get("permit_class"),
                    "description": _clip(p.get("description"), 100),
                    "value_usd": p.get("value_usd"), "issued": p.get("date_issued"),
                    "source_url": p.get("source_url")})
    return _prune(row, keep=())


def geo_row(g):
    return _prune({"id": g.get("id"), "level": g.get("level"), "name": g.get("name"),
                   "state": g.get("state")}, keep=())


def _page(r, rows, **extra):
    body = r.body
    out = {"items": rows, "next_cursor": body.get("next_cursor")}
    out.update({k: v for k, v in extra.items() if v is not None})
    out["records_charged"] = r.records
    return out


def _one(r, row, **extra):
    out = {"item": row}
    out.update({k: v for k, v in extra.items() if v is not None})
    out["records_charged"] = r.records
    return out


# ---- tools -----------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[Api, dict], dict] = field(repr=False)


TOOLS: dict[str, Tool] = {}


def _s(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def _i(desc, lo=1, hi=None, default=None):
    out = {"type": "integer", "description": desc, "minimum": lo}
    if hi is not None:
        out["maximum"] = hi
    if default is not None:
        out["default"] = default
    return out


CURSOR = _s("next_cursor from the previous answer")
GEO_ID = _s("area: CA, cty_06041, msa_41860, jur_..., ZIP 94920 (find with geo_lookup)")
DATE = "YYYY-MM-DD"


def tool(name, description, properties, required=()):
    schema: dict = {"type": "object", "properties": properties,
                    "additionalProperties": False}
    if required:
        schema["required"] = list(required)

    def deco(fn):
        TOOLS[name] = Tool(name, description, schema, fn)
        return fn
    return deco


@tool("resolve_address",
      "Find address IDs (adr_...) for street text, best match first, with permit counts. "
      "Pass an id to search_permits(address_id=...) for the permits there.",
      {"address": _s("street address, e.g. '123 Main St, San Rafael, CA 94901'"),
       "state": _s("two-letter state", pattern="^[A-Za-z]{2}$"),
       "zip": _s("five-digit ZIP", pattern=r"^\d{5}$"),
       "limit": _i("candidates", hi=20, default=5)},
      required=("address",))
def resolve_address(api, a):
    r = _fetch(api, "/v1/addresses/search", {"q": a["address"], "state": a.get("state"),
                                              "zip": a.get("zip"), "limit": a.get("limit", 5)})
    return _page(r, [address_row(x) for x in r.body.get("items") or []],
                 resolution=r.body.get("resolution"))


@tool("get_property",
      "One property (a location: address or parcel) with permit rollups. Give exactly one "
      "of: id (prp_ or adr_), address text, apn with county_fips, or lat with lon.",
      {"id": _s("prp_... or adr_..."), "address": _s("free-text address"),
       "apn": _s("parcel number, any formatting"),
       "county_fips": _s("five-digit county FIPS, required with apn", pattern=r"^\d{5}$"),
       "lat": {"type": "number", "minimum": -90, "maximum": 90},
       "lon": {"type": "number", "minimum": -180, "maximum": 180},
       "radius_m": {"type": "number", "minimum": 1, "maximum": 2000,
                    "description": "with lat/lon, default 250"}})
def get_property(api, a):
    ways = [k for k in ("id", "address", "apn", "lat") if a.get(k) is not None]
    if len(ways) != 1:
        raise ToolError("validation_error", "give exactly one of id, address, apn, lat/lon")
    if a.get("id"):
        r = _fetch(api, "/v1/properties/" + _seg(a["id"], "id"))
        return _one(r, property_row(r.body))
    if a.get("lat") is not None:
        if a.get("lon") is None:
            raise ToolError("validation_error", "lon is required with lat", "lon")
        r = _fetch(api, "/v1/properties/near", {"lat": a["lat"], "lon": a["lon"],
                                                "radius_m": a.get("radius_m"), "limit": 5})
        return _page(r, [property_row(x) for x in r.body.get("items") or []],
                     resolution=r.body.get("resolution"))
    params = ({"address": a["address"]} if a.get("address")
              else {"apn": a["apn"], "county_fips": a.get("county_fips"), "limit": 5})
    r = _fetch(api, "/v1/properties/lookup", params)
    return _page(r, [property_row(x) for x in r.body.get("items") or []],
                 resolution=r.body.get("resolution"), match=r.body.get("match_quality"))


@tool("search_permits",
      "Search permits; construction only unless category ['all'] (health, planning, "
      "licensing). date_from/to: issued, else applied, else record date.",
      {"address_id": _s("adr_..."),
       "property_id": _s("prp_..."), "geo_id": _s("CA, cty_06041, msa_41860, jur_..., "
                                                    "94920; geo_lookup"),
       "q": {"type": "string"},
       "tags": {"type": "array", "items": {"type": "string"},
                "description": "solar; -tag excludes"},
       "status": {"type": "array", "items": {"type": "string", "enum": list(PERMIT_STATUSES)}},
       "category": {"type": "array", "items": {"type": "string", "enum": list(PERMIT_CLASSES)},
                    "description": "['all'] = every class"},
       "contractor": {"type": "string"}, "contractor_id": _s("ctr_..."),
       "date_from": _s(DATE), "date_to": {"type": "string"},
       "issued_from": _s(DATE), "issued_to": {"type": "string"},
       "applied_from": _s(DATE), "applied_to": {"type": "string"},
       "min_value": {"type": "number", "minimum": 0, "description": "USD"},
       "sort": {"type": "string", "enum": list(PERMIT_SORTS)},
       "limit": _i("rows", hi=50, default=10),
       "cursor": _s("next_cursor"),
       "include_count": {"type": "boolean"}})
def search_permits(api, a):
    cat = a.get("category")
    if cat and len(cat) > 1 and "all" in cat:
        raise ToolError("validation_error", "category 'all' cannot be combined with other "
                        "classes; pass ['all'] alone or list classes", "category")
    params = {k: a.get(k) for k in ("address_id", "property_id", "geo_id", "q", "tags",
                                    "status", "category", "contractor", "contractor_id", "date_from",
                                    "date_to", "issued_from", "issued_to", "applied_from",
                                    "applied_to", "min_value", "cursor", "include_count")}
    params["sort"] = a.get("sort", "issued_desc")
    params["limit"] = a.get("limit", 10)
    r = _fetch(api, "/v1/permits/search", params)
    count = r.body.get("total_count")
    if a.get("include_count"):
        if isinstance(count, dict):
            pass
        elif isinstance(count, int) and not isinstance(count, bool):
            count = {"value": count, "relation": "eq"}
        else:
            # the API's count ran out of time; total_estimate would ignore event-date
            # windows, so it is not surfaced
            count = {"value": None, "relation": "unknown", "reason": "timeout"}
    else:
        count = None
    return _page(r, [permit_row(x) for x in r.body.get("items") or []],
                 total_count=count,
                 excluded_classes=r.body.get("excluded_classes") or None)


@tool("get_permit", "One permit by ID (pmt_...), with its source_url.",
      {"id": _s("pmt_...")}, required=("id",))
def get_permit(api, a):
    r = _fetch(api, "/v1/permits/" + _seg(a["id"], "id"))
    p = r.body
    row = permit_row(p)
    row.update(_prune({"zip5": p.get("zip5"), "apn": p.get("apn"), "owner": p.get("owner_name"),
                       "applicant": p.get("applicant_name"),
                       "license": p.get("contractor_license"),
                       "approval_days": p.get("approval_days"),
                       "construction_days": p.get("construction_days"),
                       "lat": p.get("lat"), "lon": p.get("lon")}, keep=()))
    return _one(r, row)


@tool("permit_history",
      "Status and field changes of one permit since the change feed began (2026-10-12).",
      {"id": _s("pmt_..."), "since": _s(f"{DATE} or ISO time"),
       "limit": _i("events", hi=100, default=20)},
      required=("id",))
def permit_history(api, a):
    r = _fetch(api, f"/v1/permits/{_seg(a['id'], 'id')}/history",
               {"since": a.get("since"), "limit": a.get("limit", 20)})
    rows = [_prune({"type": e.get("type"), "at": e.get("observed_at"), "field": e.get("field"),
                    "old": _clip(e.get("old"), 80), "new": _clip(e.get("new"), 80)}, keep=())
            for e in r.body.get("items") or []]
    meta = r.body.get("meta") or {}
    return _page(r, rows, permit_id=r.body.get("permit_id"),
                 history_start=meta.get("history_start"))


@tool("search_contractors",
      "Find contractors by area, name, trade tags or activity window. Or give license "
      "(with optional state, nothing else) to look one license up.",
      {"geo_id": GEO_ID, "name": _s("name words, 3+ characters"),
       "license": _s("license number (exact)"),
       "state": _s("two-letter state", pattern="^[A-Za-z]{2}$"),
       "tags": {"type": "array", "items": {"type": "string"},
                "description": "permit tags of their work, e.g. solar"},
       "permit_from": _s(DATE), "permit_to": _s(DATE),
       "sort": {"type": "string", "enum": list(CONTRACTOR_SORTS), "default": "permits_desc"},
       "limit": _i("rows", hi=50, default=10), "cursor": CURSOR})
def search_contractors(api, a):
    if a.get("license"):
        extra = sorted(set(a) - {"license", "state"})
        if extra:
            raise ToolError("validation_error", f"license looks one license up and takes only "
                            f"state; drop {', '.join(extra)} or search without license",
                            extra[0])
        r = _fetch(api, "/v1/contractors/lookup", {"license": a["license"],
                                                   "state": a.get("state")})
    else:
        params = {k: a.get(k) for k in ("geo_id", "name", "state", "tags", "permit_from",
                                        "permit_to", "cursor")}
        params.update(sort=a.get("sort", "permits_desc"), limit=a.get("limit", 10))
        r = _fetch(api, "/v1/contractors/search", params)
    return _page(r, [contractor_row(x) for x in r.body.get("items") or []])


@tool("get_contractor", "One contractor by ID (ctr_...): license, health, activity.",
      {"id": _s("ctr_...")}, required=("id",))
def get_contractor(api, a):
    r = _fetch(api, "/v1/contractors/" + _seg(a["id"], "id"))
    c = r.body
    row = contractor_row(c)
    health = c.get("license_health") or {}
    lic = c.get("license") or {}
    row.update(_prune({"legal_name": c.get("legal_name"),
                       "license_expires": health.get("expiration_date"),
                       "license_classes": lic.get("classes"),
                       "license_as_of": lic.get("as_of"),
                       "first_permit_date": c.get("first_permit_date")}, keep=()))
    return _one(r, row)


@tool("geo_lookup",
      "Find area IDs (state, metro, county, jurisdiction, ZIP) by name for geo_id filters.",
      {"q": _s("place name, e.g. 'Marin' or 'San Rafael'"),
       "level": {"type": "string", "enum": list(GEO_LEVELS)},
       "state": _s("two-letter state", pattern="^[A-Za-z]{2}$"),
       "limit": _i("rows", hi=20, default=5)},
      required=("q",))
def geo_lookup(api, a):
    r = _fetch(api, "/v1/geo/search", {"q": a["q"], "level": a.get("level"),
                                       "state": a.get("state"), "limit": a.get("limit", 5)})
    return _page(r, [geo_row(x) for x in r.body.get("data") or []])


@tool("metrics",
      "Permit counts and values by month, or by tag, class, status or jurisdiction. permits: "
      "month applied, else issued, else record date; issued/finaled: own date. At most 36 "
      "rows, 1 record per row shown.",
      {"geo_id": GEO_ID, "from": _s("YYYY-MM"), "to": _s("YYYY-MM"),
       "group_by": {"type": "string", "enum": list(METRIC_DIMS), "default": "month",
                    "description": "issued can exceed permits; search_permits uses another "
                                   "date basis"},
       "tag": _s("one work tag"), "class": _s("permit class, e.g. building")},
      required=("geo_id",))
def metrics(api, a):
    r = _fetch(api, "/v1/metrics", {"geo_id": a["geo_id"], "from": a.get("from"),
                                    "to": a.get("to"), "group_by": a.get("group_by", "month"),
                                    "tag": a.get("tag"), "class": a.get("class"),
                                    "limit": METRIC_ROWS})
    keep = ("month", "tag", "class", "status", "jurisdiction", "permits", "permits_with_value", "value_sum", "issued", "finaled", "value_p50",
            "approval_days_p50")
    rows = [_prune({k: x.get(k) for k in keep}, keep=()) for x in r.body.get("data") or []]
    meta = r.body.get("meta") or {}
    shown = rows[:METRIC_ROWS]
    available = meta.get("rows_available")
    if isinstance(available, int) and available > len(shown):
        not_shown = available - len(shown)
    else:
        not_shown = len(rows) - METRIC_ROWS if len(rows) > METRIC_ROWS else None
    return _page(r, shown, geo=r.body.get("geo"), rows_not_shown=not_shown,
                 truncated=meta.get("truncated") or None)


@tool("coverage",
      "How complete our data is for an area: permits held and field fill rates (tiers).",
      {"geo_id": GEO_ID, "from": _s("YYYY-MM"), "to": _s("YYYY-MM")},
      required=("geo_id",))
def coverage(api, a):
    r = _fetch(api, "/v1/meta/coverage", {"geo_id": a["geo_id"], "from": a.get("from"),
                                          "to": a.get("to")})
    rows = [_prune({k: x.get(k) for k in ("field", "fill_pct", "tier", "available")}, keep=())
            for x in r.body.get("items") or []]
    return _page(r, rows, permits_total=r.body.get("permits_total"),
                 agencies=r.body.get("agencies"))


@tool("search_projects",
      "Big construction projects (permits plus early signals such as CEQA filings), "
      "newest first, with stage and source_url.",
      {"geo_id": GEO_ID,
       "q": _s("every word must appear as part of a word in name, titles, descriptions, "
               "address or location (ware matches software); no quotes"),
       "min_value": {"type": "number", "minimum": 0, "description": "USD"},
       "stage": {"type": "array", "items": {"type": "string"},
                 "description": "e.g. in_review, approved, permit_issued"},
       "date_from": _s(DATE), "date_to": _s(DATE),
       "sort": {"type": "string", "enum": list(PROJECT_SORTS), "default": "newest"},
       "limit": _i("rows", hi=50, default=10), "cursor": CURSOR})
def search_projects(api, a):
    params = {k: a.get(k) for k in ("geo_id", "q", "min_value", "stage", "date_from",
                                    "date_to", "cursor")}
    params.update(sort=a.get("sort", "newest"), limit=a.get("limit", 10))
    r = _fetch(api, "/v1/projects", params)
    return _page(r, [project_row(x) for x in r.body.get("items") or []])


@tool("get_project", "One big project (prj_...) with its newest items and their source_url.",
      {"id": _s("prj_..."), "items": _i("items to return", hi=50, default=10)},
      required=("id",))
def get_project(api, a):
    r = _fetch(api, "/v1/projects/" + _seg(a["id"], "id"))
    p = r.body
    row = project_row(p)
    items = p.get("items") or []
    n = a.get("items", 10)
    row["items"] = [project_item_row(i) for i in items[:n]]
    if len(items) > n:
        row["items_not_shown"] = len(items) - n
    return _one(r, row)


@tool("search_decisions",
      "Zoning, land-use and CEQA decisions (rezoning, variance, subdivision, ...), with "
      "source_url.",
      {"geo_id": GEO_ID, "q": _s("words in title or description"),
       "category": {"type": "array", "items": {"type": "string"},
                    "description": "e.g. rezoning, variance, environmental_review"},
       "decision_from": _s(DATE), "decision_to": _s(DATE),
       "land_use": {"type": "boolean", "description": "true: land-use decisions only"},
       "limit": _i("rows", hi=50, default=10), "cursor": CURSOR})
def search_decisions(api, a):
    params = {k: a.get(k) for k in ("geo_id", "q", "category", "decision_from",
                                    "decision_to", "land_use", "cursor")}
    params["limit"] = a.get("limit", 10)
    r = _fetch(api, "/v1/decisions/search", params)
    return _page(r, [decision_row(x) for x in r.body.get("items") or []],
                 total_count=r.body.get("total_count"))


@tool("search_signals",
      "Early project signals (CEQA filings, council matters, planning cases), newest filed "
      "first, with stage, stated size, location, APNs, a short description and source_url.",
      {"geo_id": GEO_ID,
       "q": _s('every word must appear in title or description, any order; whole words, '
               'no plurals (center misses centers); "quoted phrase" = adjacent'),
       "category": {"type": "array", "items": {"type": "string"},
                    "description": "e.g. ceqa_nod, ceqa_eir, rezoning, site_plan"},
       "source": {"type": "array", "items": {"type": "string", "enum": list(SIGNAL_SOURCES)}},
       "date_from": _s("filed on or after, " + DATE), "date_to": _s(DATE),
       "large_only": {"type": "boolean", "description": "only large projects"},
       "min_value": {"type": "number", "minimum": 0, "description": "stated USD"},
       "project_id": _s("prj_..."),
       "limit": _i("rows", hi=50, default=10), "cursor": CURSOR})
def search_signals(api, a):
    params = {k: a.get(k) for k in ("geo_id", "q", "category", "source", "date_from",
                                    "date_to", "large_only", "min_value", "project_id",
                                    "cursor")}
    params["limit"] = a.get("limit", 10)
    r = _fetch(api, "/v1/signals", params)
    return _page(r, [signal_row(x) for x in r.body.get("items") or []])


@tool("get_signal", "One early signal (sig_...) with its full size fields, APNs and text.",
      {"id": _s("sig_...")}, required=("id",))
def get_signal(api, a):
    r = _fetch(api, "/v1/signals/" + _seg(a["id"], "id"))
    s = r.body
    row = signal_row(s)
    row.update(_prune({"location": s.get("location"), "applicant": s.get("applicant"),
                       "apns": sorted(set(s.get("apns") or []))[:5],
                       "case_numbers": (s.get("case_numbers") or [])[:5],
                       "lat": s.get("lat"), "lon": s.get("lon")}, keep=()))
    if s.get("title"):
        row["title"] = _clip(s.get("title"), DETAIL_TEXT_MAX)
    if s.get("description"):
        row["description"] = _clip(s.get("description"), DETAIL_TEXT_MAX)
    return _one(r, row)


@tool("recent_changes",
      "New and changed permits from the change feed, oldest first. Filter by area and tags, "
      "or pass webhook_id to reuse a saved webhook's filter. Resume with next_cursor.",
      {"webhook_id": _s("whk_...: use this saved filter"), "geo_id": GEO_ID,
       "tags": {"type": "array", "items": {"type": "string"}, "description": "-tag excludes"},
       "types": {"type": "array", "items": {"type": "string", "enum": list(CHANGE_TYPES)}},
       "fields": {"type": "array", "items": {"type": "string", "enum": list(CHANGE_FIELDS)}},
       "since": _s(f"{DATE}, ISO time or seq"), "cursor": CURSOR,
       "include_backfill": {"type": "boolean", "description": "add older newly loaded records"},
       "limit": _i("events", hi=100, default=10)})
def recent_changes(api, a):
    filt = {k: a.get(k) for k in ("geo_id", "tags", "types", "fields", "include_backfill")}
    if a.get("webhook_id"):
        hook = _fetch(api, "/v1/webhooks/" + _seg(a["webhook_id"], "webhook_id")).body
        saved = hook.get("filter") or {}
        filt = {k: v if v is not None else saved.get(k) for k, v in filt.items()}
        if "backfill" in saved and a.get("include_backfill") is None:
            filt["backfill"] = saved["backfill"]
    params = {**filt, "since": a.get("since"), "cursor": a.get("cursor"),
              "limit": a.get("limit", 10)}
    r = _fetch(api, "/v1/changes", params)
    meta = r.body.get("meta") or {}
    return _page(r, [change_row(x) for x in r.body.get("items") or []],
                 has_more=r.body.get("has_more"),
                 published_through=meta.get("published_through"))


@tool("list_tags",
      "Permit work tags (solar, roofing, adu, ...) for the tags filters, with permit counts "
      "and reliable/beta status.", {})
def list_tags(api, a):
    r = _fetch(api, "/v1/lists/tags")
    rows = [_prune({"id": x.get("id"), "description": _clip(x.get("description"), 70),
                    "status": x.get("status"), "permits": x.get("permits_count")}, keep=())
            for x in r.body.get("data") or []]
    return _page(r, rows)


@tool("my_usage", "Your plan, records used and remaining this UTC calendar month, and the reset date. Free.", {})
def my_usage(api, a):
    r = _fetch(api, "/v1/usage")
    u = r.body
    hooks = u.get("webhooks") or {}
    row = _prune({"plan": u.get("plan"), "used": u.get("used"), "limit": u.get("limit"),
                  "remaining": u.get("remaining"), "over_limit": u.get("is_over_limit"),
                  "period_start": u.get("period_start"), "reset_date": u.get("reset_date"),
                  "rate_limit_rpm": u.get("rate_limit_rpm"),
                  "last_days": (u.get("daily_usage") or [])[-7:],
                  "webhooks_paused": hooks.get("paused") or None,
                  "webhooks_paused_reason": hooks.get("reason")},
                 keep=("limit", "remaining"))
    return _one(r, row)


# ---- argument checks and dispatch -------------------------------------------------------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def _check_value(name, spec, v):
    t = spec.get("type")
    ok = isinstance(v, _TYPES.get(t, object)) and not (t in ("integer", "number")
                                                         and isinstance(v, bool))
    if not ok:
        raise ToolError("validation_error", f"{name} must be a {t}", name)
    if "enum" in spec and v not in spec["enum"]:
        raise ToolError("validation_error", f"{name} must be one of {', '.join(spec['enum'])}",
                        name)
    if t in ("integer", "number"):
        if "minimum" in spec and v < spec["minimum"]:
            raise ToolError("validation_error", f"{name} must be >= {spec['minimum']}", name)
        if "maximum" in spec and v > spec["maximum"]:
            raise ToolError("validation_error", f"{name} must be <= {spec['maximum']}", name)
    if t == "array":
        for x in v:
            _check_value(name, spec.get("items", {}), x)


def check_args(t, args):
    if not isinstance(args, dict):
        raise ToolError("validation_error", "arguments must be an object")
    props = t.schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError("validation_error", f"unknown argument {unknown[0]}; allowed: "
                        f"{', '.join(props)}", unknown[0])
    for name in t.schema.get("required", ()):
        if args.get(name) in (None, ""):
            raise ToolError("validation_error", f"{name} is required", name)
    for name, v in args.items():
        if v is not None:
            _check_value(name, props[name], v)
    return {k: v for k, v in args.items() if v is not None}


def call_tool(name, args, api):
    """(payload dict, is_error). Never raises for tool or API errors."""
    t = TOOLS.get(name)
    try:
        if t is None:
            raise ToolError("not_found", f"unknown tool {name}; tools: {', '.join(TOOLS)}")
        return t.run(api, check_args(t, args or {})), False
    except ToolError as e:
        return e.payload(), True


def to_text(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def estimate_tokens(text):
    return len(text) // 4


def list_tools():
    return [{"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in TOOLS.values()]
