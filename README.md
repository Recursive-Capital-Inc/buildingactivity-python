# buildingactivity

Source of the hosted MCP server for the [BuildingActivity](https://buildingactivity.com)
API (US building permits, properties, contractors, zoning decisions, early project
signals and big projects, each record linked to the issuer's own page, `source_url`), and
of a command line client that is not distributed yet.

Both are thin HTTP clients of `https://api.buildingactivity.com`. Callers need an API key
from [buildingactivity.com](https://buildingactivity.com/api).

## MCP server (hosted)

Customers connect to the hosted server; there is nothing to install. Endpoint:
`https://mcp.buildingactivity.com/mcp`, with the API key as an `X-API-Key` header (or
`Authorization: Bearer <key>`). Clients that can only take a URL (ChatGPT connectors,
Claude connectors without request headers) use
`https://mcp.buildingactivity.com/mcp/<your key>`; treat that URL as a secret.

18 tools with short schemas and compact answers (default page 10, every answer under
4,000 tokens): permits, properties, addresses, contractors, areas, metrics, coverage,
projects, decisions, early signals, the change feed, work tags and your usage. Setup for
each client: [docs/connect.md](docs/connect.md); tools and answers:
[docs/mcp.md](docs/mcp.md); running the hosted service: [docs/hosted.md](docs/hosted.md).

## Command line client (not distributed)

The code for a `buildingactivity` CLI lives in `src/buildingactivity/cli`, with its
reference in [docs/cli.md](docs/cli.md). It is not published to PyPI or anywhere else, so
no customer doc points at it; customers use the HTTP API or the hosted MCP server.

## Not published to PyPI

This package is not released to PyPI (owner decision). `pyproject.toml` carries the
`Private :: Do Not Upload` classifier, which PyPI refuses on upload, and the release
workflow builds and pushes the hosted MCP image only.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[mcp,dev]'
.venv/bin/pytest -q
```

## License

MIT, see [LICENSE](LICENSE). The data you get from the API is governed by the
BuildingActivity terms of service, not by this license.
