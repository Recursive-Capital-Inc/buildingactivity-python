# buildingactivity: the command line client

A small CLI over the BuildingActivity API v1. Every command is generated from the OpenAPI
document bundled with the package (`src/buildingactivity/cli/spec.json`, the /v1 part of
the API's `/openapi.json`), so commands and flags always match the API. JSON goes to
stdout, errors go to stderr as JSON, and the exit code says what happened. Stdlib plus
`requests` only.

## Install

```
pipx install buildingactivity        # or: uv tool install buildingactivity
```

`python -m buildingactivity.cli` runs the same thing.

## Key and API root

```
export BUILDINGACTIVITY_API_KEY=...         # or:
buildingactivity config set-key             # reads the key from stdin, stores it mode 0600
buildingactivity config show                # says whether a key is set, never shows it
buildingactivity config set-url https://...  # only to point at another API root
```

The key is read from `BUILDINGACTIVITY_API_KEY`, else the config file
(`$BUILDINGACTIVITY_CONFIG`, default `~/.config/buildingactivity/config.json`). There is
no `--api-key` flag, so the key never lands in shell history or `ps`. The API root comes
from `--base-url`, `BUILDINGACTIVITY_API_URL`, the config file, then the default
`https://api.buildingactivity.com`.

## Commands

Names follow the path: the words after `/v1`, plus `get` when the path ends in an ID.
When a collection and its items share a name, the collection is `list` (or `batch` when
it takes `id`). Query parameters are `--dashed` flags; path parameters are positional.
`buildingactivity schema` lists every command; `buildingactivity <command> --help` shows
each flag with its description, allowed values and an example.

| Command | Calls |
|---|---|
| `permits search --geo-id cty_06041 --tags solar` | `GET /v1/permits/search` |
| `permits search --geo-id CA --date-from 2026-09-01 --date-to 2026-09-30` | event date window (issued, else applied, else record date) |
| `permits get pmt_dzptshzmc6mlibuavdrq` | `GET /v1/permits/{permit_id}` |
| `permits batch --id pmt_dzptshzmc6mlibuavdrq,pmt_v4qlwsjau66x6wkdbxgq` | `GET /v1/permits?id=` |
| `addresses search --q "50 BARBAREE"`, `addresses get ID`, `addresses permits ID` | `/v1/addresses/...` |
| `properties get ID`, `properties lookup ...`, `properties near ...`, `properties permits ID` | `/v1/properties/...` |
| `parcels near --lat 34.0522 --lon -118.2437 --radius-m 200` | `GET /v1/parcels/near` (parcel boundaries where parcel data is loaded) |
| `contractors search --geo-id cty_06041 --tags roofing`, `contractors lookup --license ...` | `/v1/contractors/...` |
| `geo search --q Marin`, `geo states list`, `geo counties get cty_06041`, `geo zips get 94920` | `/v1/geo/...` |
| `signals list --geo-id CA --large-only --date-from 2026-09-01`, `signals sources`, `signals get sig_...` | `/v1/signals...` |
| `projects list --geo-id msa_31080 --min-value 50000000`, `projects get prj_...` | `/v1/projects...` |
| `decisions search --geo-id cty_48113 --category rezoning` | `/v1/decisions/search` |
| `metrics --geo-id cty_06041 --group-by month` | `GET /v1/metrics` |
| `webhooks list`, `webhooks get ID`, `webhooks deliveries ID` | `GET /v1/webhooks...` (read only: create and delete webhooks with a POST or DELETE to the API) |
| `meta coverage`, `meta release`, `meta data-dictionary`, `lists tags`, `usage` | free routes |

A statewide list with no date range can answer 400 `query_too_broad`; narrow it with
`--date-from` and `--date-to` or a smaller geography (county, jurisdiction, ZIP).

Extra commands:

- `property`: one property by address text, parcel (`--apn` with `--county-fips`),
  coordinates (`--lat --lon [--radius-m]`) or ID.
- `changes tail`: follows `GET /v1/changes`, one JSON line per change; at the end of the
  feed it waits `--interval` seconds (default 30) and polls again from the last cursor;
  `--once` stops when caught up, `--max-records N` after N changes. It takes the feed's
  filters as flags; backfill created events are left out unless `--include-backfill`.
- `schema [command]`: the command list, or one command's parameters, offline, no key.

## Output and pagination

Without `--limit` a list command fetches one page (the API's default size) and prints the
response as is, `next_cursor` included. `--limit N` fetches until N items, following
`next_cursor`; `--limit all` follows it to the end. `--max-records N` caps the total
whatever `--limit` says (records are what the API meters). `--page-size` sets items per
request. Pages are merged into one body: the list holds every item, `next_cursor` is where
to resume (null at the end), and `cli` reports `pages` and `records`. `--compact` prints
one line. `--dry-run` prints the request URL and makes no call (no key needed):

```
$ buildingactivity permits search --geo-id cty_06041 --tags solar --limit 10 --dry-run
https://api.buildingactivity.com/v1/permits/search?geo_id=cty_06041&tags=solar&limit=10
```

## Exit codes and errors

| Code | Meaning | error_type |
|---|---|---|
| 0 | success | |
| 1 | client error: bad flags, validation, not found | `client_error`, `validation_error`, `unsupported` |
| 2 | missing or invalid API key (401, 403) | `auth_error` |
| 3 | rate limited (429) after the retries | `rate_limited` |
| 4 | monthly records used up (402) | `credit_exhausted` |
| 5 | server error (5xx) or network failure | `server_error`, `network_error` |

Errors on stderr carry `error`, `code`, `error_type`, the HTTP `status`, the API's own
`api_error_type`, `param` and `request_id`. A 429 is retried up to `--retries` times
(default 3), waiting the `Retry-After` seconds the API sends (at most 300), else an
exponential backoff with jitter. `--no-retry` fails at once with exit 3.

## For maintainers

`spec.json` is produced, never edited: `python -m buildingactivity.cli.specsync` fetches
`https://api.buildingactivity.com/openapi.json` (or `--from <file or URL>`), keeps the /v1
paths and their schemas, drops the recorded response examples and refuses a spec that
still carries internal build markers. `tests/test_spec.py` fails when spec.json is not a
specsync render; the scheduled `spec-drift` workflow opens a PR when the live API differs.
