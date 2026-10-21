# Connect an AI assistant to BuildingActivity

BuildingActivity runs an MCP server at **`https://mcp.buildingactivity.com/mcp`**. Connect it
once and your assistant can search permits, look up properties and contractors, follow
early project signals and watch what changed, citing the issuer's own record for every
answer. It uses your API key, so what it fetches counts against your plan like any API call.

Get a key at [buildingactivity.com/api](https://buildingactivity.com/api). There are two ways
to give the server your key:

| Your client can... | Use |
|---|---|
| send a request header (Cursor, Claude Code, VS Code, Claude connectors with request headers) | URL `https://mcp.buildingactivity.com/mcp` and header `X-API-Key: <your key>` |
| only take a URL (ChatGPT, most Claude connectors) | URL `https://mcp.buildingactivity.com/mcp/<your key>` with no authentication |

A URL with your key in it is a password: do not share it, paste it in a public place or
screenshot it. If it leaks, rotate the key in your account and update the connector.

## Claude (claude.ai and Claude Desktop)

1. Open **Settings > Connectors** and choose **Add custom connector**. (On Team and
   Enterprise plans an Owner adds it under **Organization settings > Connectors**, then
   members connect it under **Settings > Connectors**.)
2. Name: `BuildingActivity`.
3. Server URL: if your dialog has a **Request headers** section, enter
   `https://mcp.buildingactivity.com/mcp`, choose **No sign-in**, and add the header
   `x-api-key` with your key. Otherwise enter `https://mcp.buildingactivity.com/mcp/<your key>`.
4. Leave the OAuth fields empty and click **Add**.
5. In a chat, turn the connector on from the tools menu and ask, for example: "Solar
   permits issued in Marin County last month, with links."

Claude Desktop can also run the server locally instead (needs [uv](https://docs.astral.sh/uv/)).
In **Settings > Developer > Edit Config**, add:

```json
{"mcpServers": {"buildingactivity": {
  "command": "uvx",
  "args": ["--from", "buildingactivity[mcp]", "buildingactivity-mcp"],
  "env": {"BUILDINGACTIVITY_API_KEY": "<your key>"}}}}
```

Restart Claude Desktop after saving.

## Claude Code

```
claude mcp add --transport http buildingactivity https://mcp.buildingactivity.com/mcp \
  --header "X-API-Key: <your key>"
```

## Cursor

Open **Cursor Settings > MCP > Add new MCP server**, or edit `~/.cursor/mcp.json`
(`.cursor/mcp.json` in a project for that project only):

```json
{"mcpServers": {"buildingactivity": {
  "url": "https://mcp.buildingactivity.com/mcp",
  "headers": {"X-API-Key": "<your key>"}}}}
```

## VS Code (GitHub Copilot agent mode)

In `.vscode/mcp.json`:

```json
{"servers": {"buildingactivity": {
  "type": "http",
  "url": "https://mcp.buildingactivity.com/mcp",
  "headers": {"X-API-Key": "${input:buildingactivity-key}"}}},
 "inputs": [{"id": "buildingactivity-key", "type": "promptString",
             "description": "BuildingActivity API key", "password": true}]}
```

## ChatGPT

Custom connectors need a plan with **Developer mode** (Plus, Pro, Business, Enterprise or
Edu; a workspace admin may have to allow it).

1. In **Settings > Apps & Connectors > Advanced settings**, turn on **Developer mode**.
2. Back in **Apps & Connectors**, choose **Create** (or **Add app**).
3. Name: `BuildingActivity`. MCP server URL: `https://mcp.buildingactivity.com/mcp/<your key>`.
4. Authentication: **No authentication** (the key in the URL is the credential).
5. Confirm you trust the app and click **Create**. In a chat, pick it from the
   **+ > Developer mode** menu.

## Other clients

Any MCP client that speaks streamable HTTP works with the URL and header above. Clients
that only run local (stdio) servers can use the local server:

```
uvx --from 'buildingactivity[mcp]' buildingactivity-mcp      # with BUILDINGACTIVITY_API_KEY set
```

## What the assistant can do

18 read-only tools: `resolve_address`, `get_property`, `search_permits`, `get_permit`,
`permit_history`, `search_contractors`, `get_contractor`, `geo_lookup`, `metrics`,
`coverage`, `search_projects`, `get_project`, `search_decisions`, `search_signals`,
`get_signal`, `recent_changes`, `list_tags` and `my_usage`. Answers are compact (a page
is 10 rows unless the assistant asks for more), and each tool call reports the records it
used. Ask "how many records do I have left?" to see your plan through `my_usage`.

## Command line

The same package has a command line client for scripts and pipelines:

```
pipx install buildingactivity
export BUILDINGACTIVITY_API_KEY=<your key>       # or: buildingactivity config set-key
buildingactivity permits search --geo-id cty_06041 --tags solar --limit 20
buildingactivity changes tail --geo-id CA --tags solar      # follow new permits
buildingactivity schema                                     # every command
```

Every API route is a command, generated from the API's own OpenAPI document. Output is
JSON; exit codes tell scripts what happened (2 bad key, 3 rate limited, 4 records used up).
