# Survey Builder Agent — MCP Server

Exposes the same 13 tools the SDK loop uses (see `src/survey_agent/tools/`)
as an [MCP](https://modelcontextprotocol.io) server over stdio, so any MCP
host — Claude Desktop, Claude Code, or another agent — can build and
publish cs14 surveys directly, without going through this repo's own
`survey-agent` CLI/loop.

## Design

`src/survey_agent/mcp_server.py` is a thin adapter, not a second
implementation:

- Tool name/description/`input_schema` come straight from `tools.TOOLS`
  (`tools/schema.py`'s `ToolSpec`) — the exact same schema the Anthropic
  tool-calling loop sends the model. Nothing is redefined or re-derived
  from a Python function signature.
- Every `tools/call` is dispatched through the same `ToolExecutor`
  (`executor.py`) the CLI uses, so `ToolError`/`CS14ApiError` map to
  `isError: true` the same way regardless of which caller is driving.
- It uses the low-level `mcp.server.lowlevel.Server` API (not
  `FastMCP`'s function-signature inference) specifically so the raw JSON
  Schema is reused byte-for-byte instead of risking drift between two
  schema sources.

## Running it

```bash
cd agent
uv sync
uv run survey-agent-mcp          # starts the MCP server on stdio
```

The server needs a cs14 backend reachable at `CS14_BASE_URL` (default
`http://localhost:8000/api/v1` — the `/api/v1` prefix is required) to log
in and make real tool calls. If the
backend is unreachable at startup, or `CS14_MOCK=1` is set, it falls back
to the same `dry_run` stub backend the CLI's `--dry-run` flag uses — so
`tools/list` and `tools/call` both work with **zero network dependencies
and no `ANTHROPIC_API_KEY`** (the MCP server itself never calls an LLM;
only the host app does).

```bash
CS14_MOCK=1 uv run survey-agent-mcp   # fully offline: no backend, no API key
```

Relevant env vars (same `.env`/`Settings` as the CLI — see `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `CS14_BASE_URL` | `http://localhost:8000/api/v1` | cs14 backend API root the server talks to |
| `CS14_EMAIL` / `CS14_PASSWORD` | demo creds | Researcher account used to log in |
| `CS14_MOCK` | unset | `1`/`true` forces dry_run mode, skipping backend auth entirely |

## Smoke test

```bash
uv run python scripts/mcp_smoke.py
```

Spawns the server over stdio with `CS14_MOCK=1`, runs MCP `initialize` +
`tools/list`, and asserts all 13 tools from `tools.TOOLS` are present with
schemas. Exits non-zero on any mismatch. No backend, no API key required.

## Claude Desktop

Add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS):

```json
{
  "mcpServers": {
    "survey-agent": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/agent", "survey-agent-mcp"],
      "env": {
        "CS14_BASE_URL": "http://localhost:8000/api/v1",
        "CS14_EMAIL": "cs14.demo@example.com",
        "CS14_PASSWORD": "change-me-client-demo"
      }
    }
  }
}
```

Use `"env": {"CS14_MOCK": "1"}` instead of the three `CS14_*` vars above to
run fully offline against the dry-run stub backend.

Restart Claude Desktop after editing the config; the 13 survey tools
should appear under the 🔌 tool icon.

## Claude Code

Add the same server via the CLI (from anywhere, using an absolute path to
this `agent/` directory):

```bash
claude mcp add survey-agent -- uv run --project /absolute/path/to/agent survey-agent-mcp
```

or add it to `.mcp.json` at a project root:

```json
{
  "mcpServers": {
    "survey-agent": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/agent", "survey-agent-mcp"],
      "env": { "CS14_MOCK": "1" }
    }
  }
}
```

Then `/mcp` inside Claude Code should list `survey-agent` with its 13
tools (`create_survey`, `update_survey`, `get_survey`, `list_surveys`,
`list_posts`, `publish_survey`, `get_share_link`, `add_post`,
`update_post_display`, `add_comment`, `add_post_question`,
`add_survey_question`, `search_handbook`).

## What's intentionally out of scope

- No SSE/HTTP transport — stdio only, matching how both Claude Desktop and
  Claude Code launch local MCP servers as a subprocess.
- No per-call auth arguments — the server logs in once at startup (or
  falls back to mock), same as the CLI; tool arguments never carry
  credentials (see `tools/auth.py`'s docstring).
- No new validation/business logic — `mcp_server.py` only adapts
  transport; any schema or handler change belongs in `tools/*.py`.
