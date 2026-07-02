# Survey Builder Agent

Turns a one-sentence natural-language instruction into a published cs14
survey by driving the cs14 REST API through Anthropic tool-calling. See
[`DESIGN.md`](./DESIGN.md) for the full design; this file is the quickstart.

Independent `uv` project. Talks to the cs14 backend over HTTP only — never
imports `backend/`.

## Setup

```bash
cd agent
uv sync
cp .env.example .env   # fill in ANTHROPIC_API_KEY / CS14_* as needed
```

## Quickstart

```bash
# Fully offline: no ANTHROPIC_API_KEY, no running backend. Replays the
# built-in demo script and stubs the backend responses.
uv run survey-agent "" --mock --dry-run

# Offline model, real backend (needs `docker compose up -d` / uvicorn running
# on CS14_BASE_URL first):
uv run survey-agent "" --mock --trace traces/mock1.jsonl

# Real model (needs ANTHROPIC_API_KEY or `ant auth login`), real backend:
uv run survey-agent "帮我建一个小红书风格的中英双语问卷，A/B两组，两个帖子加一个李克特量表，发布并给我链接" \
  --model claude-opus-4-8 --trace traces/run1.jsonl

# Point at an OpenAI-compatible proxy instead of api.anthropic.com:
uv run survey-agent "..." --base-url https://my-proxy/v1 --model some-model
```

## CLI flags

| Flag | Meaning |
|---|---|
| `instruction` (positional) | Natural-language build request (中/英). Empty string is valid with `--mock`. |
| `--mock [SCRIPT_JSON]` | Replay scripted model decisions instead of calling the API. No arg = built-in bilingual A/B demo script. With a path, loads `{"mock_script": [...]}` or a bare list. |
| `--base-url` | Override `ANTHROPIC_BASE_URL`. |
| `--cs14-base-url` | Override `CS14_BASE_URL`. |
| `--model` | Override the model id. |
| `--trace PATH` | JSONL trace output (default `traces/<ts>.jsonl`). |
| `--max-turns N` | Loop turn budget (default 20). |
| `--lang` | Preferred `default_language` hint appended to the instruction. |
| `--dry-run` | Skip the network entirely — `CS14Client` returns synthetic responses. Combine with `--mock` for a zero-dependency smoke test. |

## Env vars

See `.env.example`: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `MODEL`,
`CS14_BASE_URL`, `CS14_EMAIL`, `CS14_PASSWORD`.

## Tests

```bash
uv run pytest
```

All tests run offline (`httpx.MockTransport` for the HTTP layer,
`MockModel` for the loop) — no backend, no API key required.

## Project layout

See `DESIGN.md` §2 for the full per-file rationale. Short version:
`loop.py` is the agent loop; `model.py` is the Real/Mock model abstraction;
`tools/` is the shared tool schema + handlers (12 tools); `http_client.py`
is the only module that touches the network; `executor.py` and `trace.py`
are the dispatch/observability seams in between.

## Not implemented in this pass

- **MCP server** (`mcp_server.py` in DESIGN.md §11): `tools/schema.py` is
  written so registering `TOOLS` under `@mcp.tool()` is additive, not a
  refactor, but the server itself isn't built yet.
- **Eval harness** (`evals/`, DESIGN.md §12): tool-sequence + terminal-state
  grading against real/mock runs.
- Extended-thinking / `output_config` model params from DESIGN.md §4 —
  deferred until there's a live key to verify the behavior against.
