# MCP server

`buildingactivity-mcp` serves the BuildingActivity API v1 to agent hosts over the Model
Context Protocol, on stdio or streamable HTTP. It is a thin client of the HTTP API: every
tool is a `GET /v1/...` with your key, so auth, record metering, rate limits and the API's
error shape apply unchanged. It never reads a database.

It is built to keep agent context small: 18 tools with short closed schemas (the whole
tool list is about 3,200 tokens), compact rows instead of full objects, a default page of
10, every answer under 4,000 tokens, and one key from the environment (stdio) or from
each caller's own request (HTTP).

## Install and run

```
pipx install 'buildingactivity[mcp]'
export BUILDINGACTIVITY_API_KEY=<your key>
buildingactivity-mcp                                  # stdio
buildingactivity-mcp --transport http --port 8765     # streamable HTTP at http://127.0.0.1:8765/mcp
```

Or skip the install: `uvx --from 'buildingactivity[mcp]' buildingactivity-mcp`. The hosted
server at `https://mcp.buildingactivity.com/mcp` runs the same code; client setup is in
[connect.md](connect.md).

### Keys

- stdio: the key comes from `BUILDINGACTIVITY_API_KEY`. With no key set, every tool
  answers `auth_error` without calling the API.
- streamable HTTP: each caller sends its own key as `X-API-Key: <key>` or
  `Authorization: Bearer <key>`, or in the URL as `/mcp/<key>` for clients that can only be
  given a URL. The server forwards that key, so metering and limits apply per caller. A
  request without a key gets 401 before reaching MCP. For a single-user local setup,
  `--use-env-key` lets keyless requests use `BUILDINGACTIVITY_API_KEY` instead. The server
  binds 127.0.0.1 by default (with the SDK's DNS rebinding protection); `--allowed-host`
  names the public host when it runs behind a proxy.
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

- `records_charged` is the API's `X-Records-Request` for the call: what it cost against
  the key's monthly records. Free routes (geo, coverage, tags, usage) answer 0.
- Billing: a call is charged only for the rows it returns. `metrics` asks the API for at
  most 36 rows, so it is charged one record per row shown; `rows_not_shown` counts the
  rows left out and those are not charged.
- `next_cursor`: pass it back as `cursor` for the next page; null on the last page.
- Rows keep selected fields; nulls are dropped. Descriptions and titles are clipped to
  160 characters, other text fields to 80; IDs and URLs are never clipped.
- Record rows (permits, decisions, projects, project items, signals, changes) carry
  `source_url`, the issuer's own page; null only when the issuer gave none. Permit rows add
  `source_url_kind`: `record` (the permit's own page) or `portal` (the issuer's search page
  or dataset, when it has no per-record link).
- Errors keep the API's `type` (`validation_error`, `auth_error`, `credit_exhausted`,
  `rate_limited`, `not_found`, `unavailable`, ...) with `isError` set. Bad arguments are
  caught against the schema before any call (`records_charged: 0`).

## Tools

| Tool | API call | Default page | Notes |
|---|---|---|---|
| `resolve_address` | `GET /v1/addresses/search` | 5 | address text to `adr_` IDs, best first, with permit counts |
| `get_property` | `/v1/properties/{id}`, `/lookup`, `/near` | 1 or 5 | one of: `id`, `address`, `apn` + `county_fips`, `lat` + `lon` |
| `search_permits` | `GET /v1/permits/search` | 10 (at most 50) | `address_id`, `property_id`, `geo_id`, `q`, `tags`, `status`, `contractor`, `contractor_id`, `date_from`/`date_to` (event date: issued, else applied, else record date), issued and applied ranges, `min_value`, `sort` (default `issued_desc`), `include_count`, `category` (permit classes; default construction work only, leaving out admin, business_license, code_enforcement, engineering_row, environmental_health, other and planning; `["all"]` for every class). The answer carries `excluded_classes` when the default scope left classes out |
| `get_permit` | `GET /v1/permits/{id}` | 1 | adds owner, applicant, license, APN, coordinates, day counts |
| `permit_history` | `GET /v1/permits/{id}/history` | 20 | status and field changes since 2026-10-12 |
| `search_contractors` | `/v1/contractors/search` or `/lookup` | 10 | `license` (with optional `state`, no other filter) goes to the license lookup |
| `get_contractor` | `GET /v1/contractors/{id}` | 1 | license health and activity |
| `geo_lookup` | `GET /v1/geo/search` | 5 | names to `geo_id` values for the area filters |
| `metrics` | `GET /v1/metrics` | at most 36 rows | monthly series or one breakdown; one record charged per row shown, `rows_not_shown` counts the rest (not charged). `permits` by applied month, else issued, else record date; `issued`/`finaled` by their own date, so issued can exceed permits (search_permits uses another date basis) |
| `coverage` | `GET /v1/meta/coverage` | all fields | permits held and field fill tiers for an area |
| `search_projects` | `GET /v1/projects` | 10 | big projects with stage and first source URL |
| `get_project` | `GET /v1/projects/{id}` | 10 items | newest items with their `source_url` |
| `search_decisions` | `GET /v1/decisions/search` | 10 | zoning, land-use and CEQA decisions |
| `search_signals` | `GET /v1/signals` | 10 | early signals (CEQA, council matters, planning cases): `stage`, stated `size` (units, sqft, acres, MW, USD), `large`; filters `geo_id`, `q`, `category`, `source`, filed `date_from`/`date_to`, `large_only`, `min_value`, `project_id` |
| `get_signal` | `GET /v1/signals/{id}` | 1 | adds location, applicant, APNs, case numbers, 600 characters of text |
| `recent_changes` | `GET /v1/changes` | 10 (at most 100) | new and changed permits, oldest first; `geo_id`, `tags`, `types`, `fields`, `since`, or `webhook_id` to reuse a saved webhook's filter (one more GET); `has_more` and `next_cursor` to resume |
| `list_tags` | `GET /v1/lists/tags` | all | work tag IDs for the tag filters, permit counts, reliable or beta |
| `my_usage` | `GET /v1/usage` | 1 | plan, records used and remaining, reset date, last 7 days |

Measured answer sizes (chars / 4, `tests/test_mcp_tools.py`): on recorded API responses
35 to 330 tokens per answer; at the default page with every text field padded to 400
characters 35 to 3,416 tokens (`search_signals` the largest), all under the 4,000 budget.

## For maintainers

- `src/buildingactivity/mcp/tools.py`: the tool layer, no SDK import. `TOOLS` (name,
  description, JSON Schema, handler), `call_tool(name, args, api) -> (answer, is_error)` and
  `Api` (base URL, key, any requests-style session).
- `src/buildingactivity/mcp/server.py`: the SDK adapter (`build_server`, `http_app`,
  `main`): stdio, and stateless streamable HTTP with JSON responses, the key check, the
  `/mcp/<key>` path form and `/healthz`.
- New tool: one `@tool` function calling a `/v1` route through `_fetch`, a compact row
  function, and entries in `CALLS`, `PATHS` and `ROUTES_OF` in the tests. The schema,
  fixture-server, budget and spec tests then cover it, including that every tool argument
  is still a parameter of its route in the bundled spec.
