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
| `BUILDINGACTIVITY_API_KEY` | **never set** | a set key would not be used for callers (no `--use-env-key`), but keep it out anyway |

## Requirements on the rest of production

- The proxy must not log request URIs for this host: a key can be in the path. The
  server's own access log is off and it logs only tool name, outcome and records charged.
- The API sees every MCP call as coming from the MCP container's address. Limits must be
  keyed by API key (they are: v1 meters per key), not by client IP; any per-IP limit on
  the API must exempt the MCP container.
- `GET /healthz` answers `{"ok": true, "version": ...}` without a key; use it for the
  compose healthcheck and uptime monitoring. `POST /mcp` without a key is 401.
- DNS: an A/AAAA (or CNAME) record for `mcp.buildingactivity.com` to the production host.

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
