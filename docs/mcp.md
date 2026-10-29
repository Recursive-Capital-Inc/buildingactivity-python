# MCP server

The BuildingActivity MCP server serves the API v1 to agent hosts over the Model Context
Protocol. It is hosted at `https://mcp.buildingactivity.com/mcp` (streamable HTTP);
customers connect to it and install nothing. It is a thin client of the HTTP API: every
tool is a `GET /v1/...` with the caller's key, so auth, record metering, rate limits and
the API's error shape apply unchanged. It never reads a database.

It is built to keep agent context small: 18 tools with short closed schemas (the whole
tool list is about 3,700 tokens), compact rows instead of full objects, a default page of
10, every answer under 4,000 tokens, and each caller's own key on every request.

Client setup (Claude, ChatGPT, Claude Code, Cursor, VS Code): [connect.md](connect.md).
Running the hosted service: [hosted.md](hosted.md).

### Keys

- Each caller sends its own key as `X-API-Key: <key>` or `Authorization: Bearer <key>`, or
  in the URL as `/mcp/<key>` for clients that can only be given a URL (that URL is a
  secret; rotate the key if it leaks). The server forwards that key, so metering and
  limits apply per caller. A request without a key gets 401 before reaching MCP.
- Keys never appear in a log line or a tool answer, and the HTTP access log is off (a key
  can be in the path). The log (stderr) has one line per tool call: tool name, ok or the
  error type, records charged.

## Answers

Every tool answers one JSON object (MCP text content):

```
{"items": [...], "next_cursor": "..." | null, "records_charged": 10, ...}
{"item": {...}, "records_charged": 1}                       one-record tools
{"error": {"type", "message", "param"}, "status", "request_id", "records_charged": 0}
```

- `next_cursor`: pass it back as `cursor` for the next page; null on the last page.
- Rows keep selected fields; nulls are dropped. Descriptions and titles are clipped to
  160 characters, other text fields to 80; IDs and URLs are never clipped. The detail
  tools (`get_permit`, `get_signal`) keep up to 2,000 characters of title and
  description.
- Record rows (permits, decisions, projects, project items, signals, changes) carry
  `source_url`, the issuer's own page; null only when the issuer gave none. Permit rows add
  `source_url_kind`: `record` (the permit's own page) or `portal` (the issuer's search page
  or dataset, when it has no per-record link).
- Errors keep the API's `type` (`validation_error`, `auth_error`, `credit_exhausted`,
  `rate_limited`, `not_found`, `unavailable`, ...) with `isError` set. Bad arguments are
  caught against the schema before any call (`records_charged: 0`).

### Records

The meter counts one record per distinct row, per organization, per UTC calendar month.
The month resets at 00:00 UTC on the 1st. `records_charged` is the API's
`X-Records-Request` for the call and counts only rows new to your organization this
month.

What is one record: each permit, signal, project, contractor, property, address or
decision row is one record, and each metrics row shown is one record. `search_permits`,
`get_permit`, `recent_changes` and `permit_history` charge for their permit rows.
`permit_history` charges one record, the permit; its history entries are free. Signals from `search_signals` and `get_signal`
share one key, so pulling the same signal twice through both tools charges
once. A project's items are free with the project. `metrics` shows at most 36 rows and
is charged at most one record per row shown; `rows_not_shown` counts the rest and those
are not charged.

Reading the same record again in the same month is free, from any tool and any key of
the organization. Free tools are `geo_lookup`, `coverage`, `list_tags` and `my_usage`.
Errors are free. Permit rows inside the organization's paid area feed are free.

## Tools

| Tool | API call | Default page | Notes |
|---|---|---|---|
| `resolve_address` | `GET /v1/addresses/search` | 5 | address text to `adr_` IDs, best first, with permit counts |
| `get_property` | `/v1/properties/{id}`, `/lookup`, `/near` | 1 or 5 | one of: `id`, `address`, `apn` + `county_fips`, `lat` + `lon` |
| `search_permits` | `GET /v1/permits/search` | 10 (at most 50) | `address_id`, `property_id`, `geo_id`, `q`, `tags`, `status`, `contractor`, `contractor_id`, `date_from`/`date_to` (event date: issued, else applied, else record date), issued and applied ranges, `min_value`, `sort` (default `issued_desc`), `include_count`, `category` (permit classes; default construction work only, leaving out admin, business_license, code_enforcement, engineering_row, environmental_health, other and planning; `["all"]` for every class). `include_count` adds `total_count` {value, relation}: `eq` exact, `gte` at least 10,000, `unknown` (value null) when counting ran out of time. The answer carries `excluded_classes` when the default scope left classes out |
| `get_permit` | `GET /v1/permits/{id}` | 1 | adds license, APN, coordinates, day counts; owner and applicant names only on plans that include personal names; title and description up to 2,000 characters; dates include `record_date` |
| `permit_history` | `GET /v1/permits/{id}/history` | 20 | status and field changes since 2026-10-12 |
| `search_contractors` | `/v1/contractors/search` or `/lookup` | 10 | `license` (with optional `state`, no other filter) goes to the license lookup |
| `get_contractor` | `GET /v1/contractors/{id}` | 1 | license health and activity |
| `geo_lookup` | `GET /v1/geo/search` | 5 | names to `geo_id` values for the area filters |
| `metrics` | `GET /v1/metrics` | at most 36 rows | monthly series or one breakdown; no `from`/`to` asks for the latest 36 months ending with the current UTC month (the API returns months ascending); at most one record per row shown (re-reads in the same month are free), `records_charged` is the API's figure; `rows_not_shown` counts the rest (not charged). `permits` by applied month, else issued, else record date; `issued`/`finaled` by their own date, so issued can exceed permits (search_permits uses another date basis). `geo_id` takes no ZIP. Rows keep only their `group_by` dimension key, null for the unattributed bucket (`jurisdiction`: agencies with no jurisdiction) |
| `coverage` | `GET /v1/meta/coverage` | all fields | permits held and field fill tiers for an area |
| `search_projects` | `GET /v1/projects` | 10 | big projects with `kind` and type filters and fields (kind: site, policy, program, other; type: 12 types incl. solar_farm, energy_storage; rules only, beta), stage and first source URL |
| `get_project` | `GET /v1/projects/{id}`, `/v1/projects/batch` | 10 items, or 3 per project for `ids` | one project or up to 10 by `ids`; newest items with their `source_url`, project's `type`; answer has `missing`/`aliased` lists when the API sends them |
| `search_decisions` | `GET /v1/decisions/search` | 10 | zoning, land-use and CEQA decisions |
| `search_signals` | `GET /v1/signals` | 10 | early signals (CEQA, council matters, planning cases; national filings such as grid queues, FAA, stormwater and drilling permits when `source` is `national` or a national key such as `wy_wogcc_apd`, a national `category`, or `large_only`): `kind`, `stage`, stated `size` (units, sqft, acres, MW, USD), `large`, `location`, up to 3 `apns`, a short `description`, `type` (beta); filters `geo_id`, `q`, `category`, `source`, `kind`, `type`, filed `date_from`/`date_to`, `large_only`, `min_value`, `project_id` |
| `get_signal` | `GET /v1/signals/{id}`, `/v1/signals/batch` | 1, or up to 20 by `ids` | one signal or up to 20 by `ids`; adds applicant only on plans that include personal names, case numbers, lat/lon, up to 5 APNs and up to 2,000 characters of title and description; answer has `missing`/`not_returned`/`out_of_scope` lists when the API sends them |
| `recent_changes` | `GET /v1/changes` | 10 (at most 100) | new and changed permits, oldest first; `geo_id`, `tags`, `types`, `fields`, `since`, or `webhook_id` to reuse a saved webhook's filter (one more GET); changed events carry `permit_id`, `field`, `old` and `new` only (`get_permit` gives the rest); `has_more` and `next_cursor` to resume |
| `list_tags` | `GET /v1/lists/tags` | all | work tag IDs for the tag filters, permit counts, reliable or beta |
| `my_usage` | `GET /v1/usage` | 1 | plan, records used and remaining, reset date, last 7 days |

Text search (`q`): on `search_permits` and `search_signals` every word must appear
(whole words, no plurals: `center` misses `centers`), in any order, and a quoted
`"data center"` matches the words adjacent as a phrase. On `search_projects` every word
must appear as part of a word (`ware` matches `software`) in the name, titles,
descriptions, address or location, and quotes are not supported.

Measured answer sizes (chars / 4, `tests/test_mcp_tools.py`): on recorded API responses
35 to 330 tokens per answer; at the default page with every text field padded to 400
characters 35 to 3,942 tokens (`search_signals` the largest), all under the 4,000 budget.

## For maintainers

- `src/buildingactivity/mcp/tools.py`: the tool layer, no SDK import. `TOOLS` (name,
  description, JSON Schema, handler), `call_tool(name, args, api) -> (answer, is_error)` and
  `Api` (base URL, key, any requests-style session).
- `src/buildingactivity/mcp/server.py`: the SDK adapter (`build_server`, `http_app`,
  `main`): stateless streamable HTTP with JSON responses (what the hosted service runs),
  the key check, the `/mcp/<key>` path form and `/healthz`. The stdio transport is kept
  for development and tests only and is not offered to customers.
- New tool: one `@tool` function calling a `/v1` route through `_fetch`, a compact row
  function, and entries in `CALLS`, `PATHS` and `ROUTES_OF` in the tests. The schema,
  fixture-server, budget and spec tests then cover it, including that every tool argument
  is still a parameter of its route in the bundled spec.
