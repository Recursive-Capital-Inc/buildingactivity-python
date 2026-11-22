# buildingactivity

Command line client and MCP server for the [BuildingActivity](https://buildingactivity.com)
API: US building permits, properties, contractors, zoning decisions, early project signals
and big projects, each record linked to the issuer's own page (`source_url`).

Both tools are thin HTTP clients of `https://api.buildingactivity.com`. You need an API key
from [buildingactivity.com](https://buildingactivity.com/api).

## Install

```
pipx install buildingactivity            # the CLI: `buildingactivity`
pipx install 'buildingactivity[mcp]'     # adds the MCP server: `buildingactivity-mcp`
```

Or with uv: `uv tool install buildingactivity` (add `--with mcp --with uvicorn`, or install
`'buildingactivity[mcp]'`). Python 3.11 or newer. The CLI needs only `requests`.

## CLI

```
export BUILDINGACTIVITY_API_KEY=...          # or: buildingactivity config set-key (reads stdin)
buildingactivity permits search --geo-id cty_06041 --tags solar --limit 20
buildingactivity property --address "50 Barbaree Way, Tiburon, CA 94920"
buildingactivity signals list --geo-id CA --large-only
buildingactivity changes tail --geo-id cty_04013 --tags solar
buildingactivity usage
buildingactivity schema                      # every command, offline
```

Commands are generated from the API's OpenAPI document bundled with the package, so every
API route and parameter has a command and a flag. JSON on stdout, JSON errors on stderr,
exit codes 0 ok, 1 client error, 2 auth, 3 rate limited, 4 records used up, 5 server or
network. Details: [docs/cli.md](docs/cli.md).

## MCP server

Hosted, nothing to install: `https://mcp.buildingactivity.com/mcp` with your key as an
`X-API-Key` (or `Authorization: Bearer`) header. Clients that can only take a URL
(ChatGPT connectors, Claude connectors without header support) use
`https://mcp.buildingactivity.com/mcp/<your key>`; treat that URL as a secret.

Local, over stdio (Claude Desktop, Cursor and most hosts):

```json
{"mcpServers": {"buildingactivity": {
  "command": "uvx", "args": ["--from", "buildingactivity[mcp]", "buildingactivity-mcp"],
  "env": {"BUILDINGACTIVITY_API_KEY": "<your key>"}}}}
```

18 tools with short schemas and compact answers (default page 10, every answer under
4,000 tokens): permits, properties, addresses, contractors, areas, metrics, coverage,
projects, decisions, early signals, the change feed, work tags and your usage. Setup for
each client: [docs/connect.md](docs/connect.md); tools and answers:
[docs/mcp.md](docs/mcp.md); running the hosted service: [docs/hosted.md](docs/hosted.md).

## License

MIT, see [LICENSE](LICENSE). The data you get from the API is governed by the
BuildingActivity terms of service, not by this license.
