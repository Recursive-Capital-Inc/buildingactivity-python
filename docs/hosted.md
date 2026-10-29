# Running the hosted MCP service (mcp.buildingactivity.com)

The hosted service is the same `buildingactivity-mcp` server in streamable HTTP mode, in
a container, behind the production reverse proxy. It holds no key of its own: every
request carries its caller's key (`X-API-Key`, `Authorization: Bearer`, or `/mcp/<key>`),
which it forwards to the API, so metering, plan limits and rate limits are the API's,
per caller. It is stateless (no MCP sessions kept), so it scales by adding replicas.

## Pieces

| File | What |
|---|---|
| `Dockerfile` | python:3.12-slim, the wheel plus pinned deps (`deploy/requirements-mcp.txt`), non-root user 10001, `HEALTHCHECK` on `/healthz` |
| `deploy/compose.mcp.yml` | the `mcp` service to copy into the production compose: read-only root, all capabilities dropped, 256 MB, 1 CPU, log rotation |
| `deploy/Caddyfile.mcp` | proxy block for `mcp.buildingactivity.com` with request logging off |
| `.github/workflows/release.yml` | on a GitHub release, pushes `ghcr.io/recursive-capital-inc/buildingactivity-mcp:<version>` and `:latest` |

## Settings

| Variable | Production value | Why |
|---|---|---|
| `BUILDINGACTIVITY_API_URL` | the API service on the compose network, e.g. `http://api:8451` | stays inside the host network; no TLS hop or public egress |
| `BUILDINGACTIVITY_MCP_ALLOWED_HOSTS` | `mcp.buildingactivity.com` | the SDK's DNS rebinding protection: other Host headers get 421 |
| `FORWARDED_ALLOW_IPS` | the proxy's address, or `*` when only the proxy can reach the container | trust X-Forwarded-* from the proxy only |
| `BUILDINGACTIVITY_MCP_USAGE_DIR` | `/usage` (bind mount of `/srv/mcp/usage`) | durable usage events, one `usage-YYYY-MM-DD.jsonl` per UTC day; unset = stderr only |
| `BUILDINGACTIVITY_MCP_USAGE_KEEP_DAYS` | `400` (the default) | usage files older than this many days are deleted at start and once a day |
| `BUILDINGACTIVITY_API_KEY` | **never set** | a set key would not be used for callers (no `--use-env-key`), but keep it out anyway |

## Requirements on the rest of production

- The proxy must not log request URIs for this host: a key can be in the path. The
  server's own access log is off; it logs only the usage events below.
- The API sees every MCP call as coming from the MCP container's address. Limits must be
  keyed by API key (they are: v1 meters per key), not by client IP; any per-IP limit on
  the API must exempt the MCP container.
- `GET /healthz` answers `{"ok": true, "version": ...}` without a key; use it for the
  compose healthcheck and uptime monitoring. `POST /mcp` without a key is 401.
- DNS: an A/AAAA (or CNAME) record for `mcp.buildingactivity.com` to the production host.

## Usage events

Every tool call and every MCP `initialize` leaves one JSON object, on the logger
`buildingactivity.mcp.usage` (stderr) and, when `BUILDINGACTIVITY_MCP_USAGE_DIR` is set,
appended to `<dir>/usage-YYYY-MM-DD.jsonl` (UTC day, file mode 0640). Timestamps are UTC
ISO 8601 with milliseconds. A write error logs one warning and never fails a tool call.

```
{"ts":"2026-10-28T20:41:07.123Z","event":"initialize","key_ref":"3f1c0a9d2b7e4c18","key_via":"path",
 "client_name":"claude-ai","client_version":"0.1.0","protocol_version":"2025-06-18","user_agent":"..."}
{"ts":"2026-10-28T20:41:07.456Z","event":"tool_call","key_ref":"3f1c0a9d2b7e4c18","key_via":"path",
 "tool":"search_permits","outcome":"ok","status":null,"latency_ms":182.4,"records_charged":10,
 "args":{"address_id":24,"limit":null},"client_name":"claude-ai","client_version":"0.1.0",
 "user_agent":"...","protocol_version":"2025-06-18","server_version":"0.1.0"}
```

What an event holds:

- `key_ref`: the first 16 hex characters of sha256(key). The API's key store keeps the full
  sha256 (`keys.txt` lines `sha256:<hex>`, the accounts database), so an operator maps a ref
  to a key label or org offline. `key_via`: `header` (X-API-Key), `bearer` or `path`.
- `tool` (`unknown` for a name the server does not have), `outcome` (`ok` or the error type),
  `status` (the API's HTTP status on an error), `latency_ms` (around the tool call),
  `records_charged`.
- `args`: argument NAME to SIZE (length of a string, list or object; `null` for numbers,
  booleans and null). Names outside the tool's schema are only counted, as `_other`.
- `client_name`, `client_version`: from the last `initialize` with the same key and user
  agent within 24 hours (the server is stateless, so a tool call carries no client info;
  an in-memory LRU of 4,096 entries joins them). `null` when unknown.
- `user_agent` (first 120 characters), `protocol_version` (`MCP-Protocol-Version` header),
  `server_version`.

What it never holds: the key or any part of the raw key, any argument value, the request
path, IP addresses.

Retention: 400 days (`BUILDINGACTIVITY_MCP_USAGE_KEEP_DAYS`). The directory must survive a
container recreation (the bind mount) and be in the host's nightly backup.

**Deploy note.** Production's `/srv/mcp/compose.yaml` needs the same mount and env on the
next MCP deploy: `mkdir -p /srv/mcp/usage && chown 10001:10001 /srv/mcp/usage && chmod 750
/srv/mcp/usage`, then add to the `mcp` service `BUILDINGACTIVITY_MCP_USAGE_DIR: /usage` and
`volumes: ["/srv/mcp/usage:/usage"]`, rebuild and recreate it. Without the mount the events
reach only the container log, which is lost on every recreate.

## Smoke test

```
docker compose -f deploy/compose.mcp.yml up --build -d
curl -s http://127.0.0.1:8765/healthz
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Host: mcp.buildingactivity.com' \
  -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8765/mcp      # 401
```

With a real key, an MCP `initialize` then `tools/call my_usage` through
`https://mcp.buildingactivity.com/mcp` must return your plan; `my_usage` is free.

## Later

- OAuth 2.1 sign-in (so Claude and ChatGPT users connect with their BuildingActivity
  account instead of a key in the URL, and the connector can be listed in their
  directories) needs an authorization server on the account side; the MCP SDK's
  `auth` settings take a token verifier for it.
