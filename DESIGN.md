# Survey Builder Agent — Technical Design

> Status: design v1 · Branch: `feat/survey-builder-agent` · Scope: `cs14/agent/` (independent `uv` project, talks to the cs14 backend over HTTP only, never imports `backend/`)

## 0. TL;DR

A researcher types one natural-language sentence (中/英) — e.g. *"帮我建一个小红书风格的问卷，中英双语，A/B 两组：一组显示点赞数一组不显示，加两个刺激帖子和一个 5 点李克特量表，发布并给我分享链接"* — and the agent completes the whole build chain against the cs14 REST API through multiple rounds of function calling:

```
建问卷(语言集/AB组一次性定型) → 配 display_* 覆盖 → 加多语言帖子刺激物 → 加评论 → 加问题块(post级/survey级) → 发布 → 返回 /survey/{share_code}?lang=…
```

Design pillars, each defended in its own section:

1. **No framework.** Anthropic Python SDK directly (`base_url` + `model` overridable for future compatible endpoints), `httpx` for the tool layer. §4, §5.
2. **Minimalist (badlogic/pi philosophy):** 13 tools (12 platform API + 1 local RAG handbook search), short system prompt, one explicit readable loop. §6, §7.
3. **`--mock` mode is first-class:** a `MockModel` replays scripted `tool_use` decisions; the tool layer still fires real HTTP. Enables offline/CI/regression without an API key (env has no `ANTHROPIC_API_KEY`). §8.
4. **Full-run trace:** JSONL per run — per-round model-input digest, tool call + result digest, token usage, latency, estimated cost. §9.
5. **Robust failure handling:** HTTP retry w/ exponential backoff, tool-result truncation, context-window trimming, model-side degradation path. §10.
6. **MCP server reuses the exact same tool definitions** via a single shared schema source. §11.
7. **Two-layer eval:** tool-sequence match + terminal-state assertion, runnable under mock and real model. §12.

---

## 1. Why an agent (and why these constraints)

The build chain is **multi-step, order-sensitive, and partially irreversible** — a textbook agent case, not a single call:

- Language set and A/B group config are **locked after publish** (`platform_style, platform_ui_style, num_groups, group_names, *_tracking_*, calibration_*, default_language, supported_languages` → 409 once `status=="published"`). So the agent must decide these *up front* from a fuzzy sentence.
- Posts/questions become **immutable once a non-preview participant responds** (409). The whole chain must finish before anyone real enters.
- Natural language is fuzzy ("双语"→ which two? "A/B"→ how many groups, what names?); the model must resolve intent into a concrete, valid API payload sequence, and recover when the API pushes back (422/409).

That is exactly what tool-use + a decision loop is for. We deliberately **do not** use Managed Agents (server-side loop) — we host the loop ourselves because (a) it's a portfolio piece meant to show the loop is understood, (b) mock mode requires intercepting the decision layer, and (c) the tool layer must hit a self-hosted backend, not an Anthropic sandbox.

---

## 2. Directory structure & per-file responsibility

```
cs14/agent/
├── pyproject.toml              # uv project; deps: anthropic, httpx, mcp, pytest, python-dotenv
├── uv.lock
├── README.md                   # quickstart, the demo one-liner, env vars
├── DESIGN.md                   # this document
├── .env.example                # ANTHROPIC_API_KEY?, ANTHROPIC_BASE_URL?, MODEL, CS14_BASE_URL, CS14_EMAIL, CS14_PASSWORD
│
├── src/survey_agent/
│   ├── __init__.py
│   ├── config.py               # Settings dataclass: model id, base_urls, creds, retry/backoff knobs,
│   │                           #   truncation limits, context budget, price table. env + CLI override.
│   │
│   ├── cli.py                  # `uv run survey-agent` entrypoint. argparse: instruction (positional),
│   │                           #   --mock [SCRIPT], --base-url, --model, --trace PATH, --max-turns,
│   │                           #   --lang, --dry-run. Wires Settings→Model→ToolExecutor→Loop→Trace.
│   │
│   ├── loop.py                 # THE agent loop (see §6). ~120 lines, explicit, no hidden control flow.
│   │                           #   run(instruction) -> RunResult. Owns turn counter, message history,
│   │                           #   stop conditions, context trimming, and the model-error degradation path.
│   │
│   ├── model.py                # Model abstraction (§8):
│   │                           #   - Protocol `Model.complete(system, messages, tools) -> ModelResponse`
│   │                           #   - RealModel: wraps anthropic.Anthropic (base_url + model injected)
│   │                           #   - MockModel: replays a script of tool_use / final-text decisions
│   │                           #   ModelResponse normalizes content blocks + usage across both.
│   │
│   ├── tools/
│   │   ├── __init__.py         # TOOLS: the single ordered list of ToolSpec (shared by SDK loop AND MCP).
│   │   ├── schema.py           # ToolSpec dataclass {name, description, input_schema, handler}.
│   │   │                       #   `anthropic_tools()` -> SDK tool dicts; `mcp_tools()` -> MCP registration.
│   │   ├── survey.py           # handlers: create_survey, update_survey, get_survey, list_surveys,
│   │   │                       #   publish_survey  (map 1:1 onto §3.1/§3.4 endpoints, encode the "坑")
│   │   ├── content.py          # handlers: add_post, update_post_display, add_comment,
│   │   │                       #   add_post_question, add_survey_question
│   │   └── auth.py             # handler-side: ensure_researcher() login/register bootstrap (not a model tool)
│   │
│   ├── http_client.py          # CS14Client: thin httpx wrapper. JWT bearer, login/register,
│   │                           #   retry+backoff (§10), pagination helpers (list vs bare-array),
│   │                           #   raises CS14ApiError{status, body} that handlers turn into tool_result.
│   │
│   ├── executor.py             # ToolExecutor: name→handler dispatch, arg validation against schema,
│   │                           #   result truncation (§10), exception→is_error tool_result mapping.
│   │
│   ├── trace.py                # Tracer: JSONL writer. One event per round; RunSummary at end. Cost calc.
│   │
│   ├── prompts.py              # SYSTEM_PROMPT (short, §6.3) + the language/AB heuristics text.
│   │
│   └── mcp_server.py           # Low-level `mcp.server.lowlevel.Server` exposing the same TOOLS over MCP (stdio), not FastMCP. `uv run survey-agent-mcp`.
│
├── scripts/
│   ├── demo.sh                 # end-to-end demo: bootstrap backend, seed researcher, run 3 instructions
│   └── run_evals.sh            # runs the eval suite (mock by default, --real to use the model)
│
├── evals/
│   ├── cases/                  # one JSON per case (§12): instruction + mock script + expected seq + asserts
│   │   ├── 01_bilingual_ab_xhs.json
│   │   ├── 02_minimal_en_single.json
│   │   └── 03_recover_from_422.json
│   ├── runner.py               # loads cases, runs loop, applies the two-layer grader, prints scorecard
│   └── graders.py              # sequence_match() + terminal_state_assert()
│
└── tests/
    ├── test_http_client.py     # retry/backoff, pagination shapes, error mapping (httpx MockTransport)
    ├── test_executor.py        # truncation, arg validation, is_error mapping
    ├── test_loop_mock.py       # loop drives to terminal state under MockModel, no network (handlers stubbed)
    └── test_schema_parity.py   # TOOLS → anthropic_tools() and mcp_tools() stay in sync
```

**Rationale for the split:** `model.py`, `loop.py`, `tools/`, `trace.py` are independently testable seams. The loop never imports `anthropic` or `httpx` directly — it depends on the `Model` protocol and the `ToolExecutor`, so a full run is exercisable with zero network and zero API key. This is what makes `--mock` cheap and CI-safe.

---

## 3. Backend integration facts encoded into the tools

These come from the recon report and are **baked into handlers/schemas so the model can't get them wrong**:

| Concern | Encoding in the agent |
|---|---|
| `share_code` exists at create time | `create_survey` returns it immediately; loop threads it into the final link even before publish. |
| Publish-locked fields | `create_survey` schema surfaces `num_groups/group_names/supported_languages/default_language` as first-class inputs; system prompt says "define A/B + languages before any post". `update_survey` handler pre-checks `status` and returns a structured `is_error` result explaining the lock rather than blindly 409-ing. |
| Language normalization + Arabic removed | `create_survey` schema `enum`: `["en","zh-CN","zh-TW","ja","ko","es"]`; handler normalizes `zh`/`zh-cn`→`zh-CN`, `zh-tw`→`zh-TW` client-side before send. |
| No "manual post" endpoint | `add_post` takes only `{original_url, order}`; `update_post_display` is a separate tool for `display_*` / fake-interaction / `visible_to_groups` / `group_overrides`. Prompt: "for made-up stimuli, add_post with a real public URL (or a placeholder) then update_post_display to set the shown content." |
| OG fetch SSRF-blocks localhost/private IPs (silent) | `add_post` handler flags in its result whether `fetched_*` came back null, nudging the model to call `update_post_display`. |
| `create_post/questions` 409 after real responses | ordering enforced by prompt + loop guardrail: publish is the last mutating call. |
| single/multiple_choice options validated only at answer time; likert/rating min<max at create time | `add_*_question` handler client-side-validates `config.options` non-empty and `0<=min<max`, returning `is_error` early instead of shipping a broken question. |
| Duplicate compat route for post questions | handler only ever uses the non-duplicated path. |
| `/auth/login` is JSON body, not OAuth form | `CS14Client.login()` posts `{email,password}` JSON. |
| pagination: `/surveys` = `{items,total}`, `/posts` = bare array | `http_client` has two typed helpers; `list_surveys` uses the wrapped one. |
| 404 (not 403) on cross-user access | handlers treat 404 on an owned id as "gone/not-owned", surfaced as `is_error`. |

---

## 4. Model client (SDK, base_url + model overridable)

`RealModel` wraps `anthropic.Anthropic(base_url=..., api_key=...)`. Model id and `base_url` come from `Settings` (env `MODEL`, `ANTHROPIC_BASE_URL`), so pointing at an OpenAI-compatible/proxy endpoint later is a config change, not a code change.

- **Model:** default `claude-opus-4-8` (Opus 4.8, $5/$25 per 1M tok, 1M ctx). Overridable to `claude-sonnet-5` ($3/$15) for cheaper eval runs or `claude-haiku-4-5` for smoke.
- **Thinking:** `thinking={"type":"adaptive"}` — the build chain is multi-step planning; adaptive lets Claude decide depth per turn. `output_config={"effort":"high"}` (agentic default). No `budget_tokens` (removed on 4.8).
- **max_tokens:** 8000 non-streaming (well under the 16K/HTTP-timeout guidance; tool-use turns are short).
- **tool_choice:** `{"type":"auto"}` — the loop must let the model both call tools and emit a final text summary with the share link.
- **Auth:** `Anthropic()` resolves `ANTHROPIC_API_KEY` or an `ant auth login` profile; if neither is present and `--mock` is not set, `cli.py` fails fast with a clear message pointing at `--mock`.

`RealModel.complete()` returns a normalized `ModelResponse{ text_blocks, tool_uses:[{id,name,input}], stop_reason, usage:{input,output,cache_read,cache_creation} }`. `MockModel.complete()` returns the same shape from a script — **the loop cannot tell them apart.**

---

## 5. HTTP tool layer (`httpx`)

`CS14Client` is the only thing that touches the network. Responsibilities:

- **Auth bootstrap:** `ensure_researcher()` tries `login(email,password)`; on 401 falls back to `register` then `login`. Stores the bearer token; every request sends `Authorization: Bearer <jwt>`.
- **Requests:** typed methods per endpoint (`create_survey`, `patch_survey`, `create_post`, `patch_post`, `add_comment`, `create_post_question`, `create_survey_question`, `publish`, `get_survey`, `list_surveys`). Handlers in `tools/*.py` are thin adapters: validate model args → call client method → shape a compact `tool_result`.
- **Errors:** non-2xx → `CS14ApiError(status, body_json)`. The handler decides whether that's a retryable transport error (bubble to retry) or a semantic 4xx (return `is_error` tool_result so the model can adjust — e.g. 422 invalid language, 409 locked field).
- **Retry/backoff:** see §10.

Handlers are pure functions `(client, args) -> dict`; that's what lets the same handler back both the SDK loop and the MCP server.

---

## 6. The agent loop

### 6.1 Shape (explicit, ~120 lines)

```
run(instruction):
    messages = [user(instruction)]
    for turn in range(max_turns):
        trim_context(messages)                       # §10
        resp = model.complete(SYSTEM, messages, anthropic_tools())
        trace.round(turn, model_input_digest, resp.usage, latency)
        messages.append(assistant(resp.raw_content))  # preserve tool_use blocks verbatim

        if resp.stop_reason != "tool_use":
            return RunResult(final_text=resp.text, state=collect_state())   # done

        tool_results = []
        for tu in resp.tool_uses:                    # execute all (parallel-safe on read tools)
            result = executor.run(tu.name, tu.input)  # truncated, is_error-aware
            trace.tool(tu.name, tu.input_digest, result_digest, is_error)
            tool_results.append(tool_result_block(tu.id, result))
        messages.append(user(tool_results))          # ALL results in ONE user message
    return RunResult(final_text=None, state=collect_state(), reason="max_turns")
```

Key invariants (each a defensible design choice under interview):
- **Append full `resp.raw_content`** (not just text) so `tool_use` blocks survive the round-trip.
- **All `tool_result` blocks in a single user message** — splitting them trains the model to stop parallelizing.
- **A failed tool returns a `tool_result` with `is_error:true`, never a dropped block** — every `tool_use` id must get a matching result or the next request 400s.
- **Terminal condition** = model emits final text (no tool_use) OR `max_turns` hit. `collect_state()` snapshots `{survey_id, share_code, status, posts, questions}` accumulated by handlers into a shared `RunContext`, which the grader and the final link builder both read.

### 6.2 Stop / guardrails
- `--max-turns` default 20 (build chain is ~8–12 tool calls; headroom for one recovery loop).
- A soft guardrail: if `publish_survey` succeeds, the loop hints the model (system reminder appended as a `role:"system"` message on Opus 4.8) that the task is complete and it should return the share link — prevents over-running.

### 6.3 System prompt (short — the whole thing, ~200 words)
> You are a survey-building assistant for the cs14 platform. Turn the researcher's request into a published survey by calling tools. Rules: (1) Decide the language set and A/B group count/names FIRST and pass them to `create_survey` — they cannot be changed after publish. Default languages inferred from the request; valid codes: en, zh-CN, zh-TW, ja, ko, es. (2) To add a stimulus post, call `add_post` with a URL; if you are inventing the content or the fetch returns nothing, follow with `update_post_display` to set the shown title/image/text and any fake like/comment counts and per-group visibility. (3) Add question blocks with `add_post_question` (attached to a post) or `add_survey_question` (standalone). single/multiple_choice need non-empty options; likert/rating need 0<=min<max. (4) `publish_survey` LAST, only after at least one post exists. (5) When done, reply with the share link `/survey/<share_code>?lang=<default>` and a one-line summary. If a tool returns an error, read it and adjust — do not retry blindly.

Language/AB heuristics (bilingual detection, "点赞/likes" → group_overrides) live in `prompts.py` as an appendable block, kept out of the frozen prefix for cache stability.

---

## 7. Tool catalog (13 — few and precise)

| # | Tool | Input (abridged) | Backend call |
|---|---|---|---|
| 1 | `create_survey` | title, description?, platform_style, platform_ui_style, num_groups, group_names?, default_language, supported_languages[] | `POST /surveys` |
| 2 | `update_survey` | survey_id, {any draft-mutable field} | `PATCH /surveys/{id}` |
| 3 | `get_survey` | survey_id | `GET /surveys/{id}` |
| 4 | `list_surveys` | status?, limit?, offset? | `GET /surveys` |
| 5 | `add_post` | survey_id, original_url, order | `POST /surveys/{id}/posts` |
| 6 | `update_post_display` | survey_id, post_id, display_title?, display_image_url?, display_description?, source_label?, display_likes?, show_likes?, …, visible_to_groups?, group_overrides? | `PATCH …/posts/{pid}` |
| 7 | `add_comment` | survey_id, post_id, author_name, text, author_avatar_url? | `POST …/posts/{pid}/comments` |
| 8 | `add_post_question` | survey_id, post_id, question_type, text, order, config? | `POST …/posts/{pid}/questions` |
| 9 | `add_survey_question` | survey_id, question_type, text, order, config? | `POST /surveys/{id}/questions` |
| 10 | `publish_survey` | survey_id | `POST /surveys/{id}/publish` |
| 11 | `list_posts` | survey_id, limit?, offset? | `GET …/posts` |
| 12 | `get_share_link` | survey_id, language? | (local) builds `/survey/{share_code}?lang=…` from RunContext |
| 13 | `search_handbook` | query, top_k? | (local) RAG over `docs/` + `docs-site/` handbook: BM25 always; + Ollama `bge-m3` hybrid fused via RRF when reachable (see `rag/`) |

Why not more: translation import/export, analytics, close/reopen are out of the core build chain — exposing them would dilute the model's tool-selection accuracy. They can be added later as a second toolset gated behind a flag.

Each `ToolSpec` carries a strict `input_schema` (`additionalProperties:false`, `required`, enums for `platform_ui_style`, `question_type`, language codes). Strict schemas make argument errors a compile-time-ish 422 the handler catches, not a silent bad payload.

---

## 8. Mock mode (`--mock`)

`MockModel` reads a **script**: an ordered list of "turns", each turn either a list of `tool_use` decisions or a final text. On each `complete()` call it pops the next turn and returns it in `ModelResponse` shape, ignoring `messages`/`tools`. Two script sources:

1. `--mock evals/cases/XX.json` → uses that case's embedded `mock_script`.
2. `--mock` with no arg → a built-in default script that runs the canonical bilingual A/B build (for smoke/demo without a key).

Crucially, **the tool layer still fires real HTTP** in mock mode (unless `--dry-run`). This is the design's backbone: mock mode is not a fake of the whole system, only of the *model's decisions*. So a mock run exercises the real backend, real retries, real pagination, real 409/422 handling — it just doesn't pay for or depend on the LLM. That makes it the substrate for CI and regression: same script → same tool sequence → assert terminal state.

`MockModel` also supports **conditional branches** keyed on the last tool result (e.g. "if `add_post` result shows `fetched_title==null`, next emit `update_post_display`"), so a script can exercise the recovery path deterministically.

Token usage in mock mode is reported as zeros; cost = 0; trace still emitted (proves the trace pipeline independent of the model).

---

## 9. Trace (JSONL per run)

`Tracer` writes one JSONL file per run (`--trace path`, default `traces/<ts>-<survey_id>.jsonl`). Event types:

- `run_start` — instruction, model id, base_url, mock flag.
- `round` — turn index, `model_input_digest` (message count, last-user preview, tool count), `usage{input,output,cache_read,cache_creation}`, `latency_ms`, `est_cost_usd`.
- `tool_call` — name, `input_digest` (truncated args), `result_digest` (truncated result), `is_error`, `http_status`, `duration_ms`.
- `run_summary` — total turns, total tokens by kind, total est cost, final `status`, `share_code`, share link, terminal reason.

Cost is computed from a price table in `config.py` keyed by model id (`opus-4-8: in $5 / out $25 per 1M`; cache-read at 0.1×). Digests keep the JSONL small and PII-light while staying debuggable. The trace is both a debugging tool and the artifact an interviewer can inspect to see the loop's decisions.

---

## 10. Failure handling

**Tool HTTP errors — retry with exponential backoff.** `CS14Client` retries on connection errors and status ∈ {429, 500, 502, 503, 504}: delays `base*2^n + jitter`, capped, `max_retries=3` (config). `retry-after` honored on 429. **4xx semantic errors (400/404/409/422) are NOT retried** — they're returned as `is_error` tool_results so the model corrects course (wrong language code, locked field, missing options).

**Tool-result truncation.** `executor` caps each `tool_result` string at `TOOL_RESULT_MAX_CHARS` (default 4000). Oversized results (e.g. a `list_posts` with many posts) are summarized to `{count, first_n_ids, truncated:true}` rather than dumped, keeping context lean.

**Context-window trimming.** `trim_context()` runs before each `complete()`. Budget-based: if the estimated token count of `messages` exceeds `CONTEXT_BUDGET` (default 120K, far under 1M but keeps latency/cost sane), it drops the oldest *completed* tool_use/tool_result *pairs* (never orphaning an id), preserving the original user instruction and the most recent N rounds. Because the survey state lives in `RunContext` (not only in the transcript), trimming old rounds doesn't lose the `survey_id`/`share_code`.

**Model-side error degradation.** `RealModel.complete()` catches:
- `RateLimitError`/`5xx` → SDK already retries; on exhaustion the loop surfaces a `run_summary` with `reason="model_unavailable"` and, if configured, retries once on a fallback model (`MODEL_FALLBACK`, e.g. `claude-sonnet-5`) — cache is cold but the run completes.
- `stop_reason=="refusal"` → loop stops, emits a clear failure summary (won't happen on benign survey building, but handled so `content[0]` access never crashes).
- Malformed/empty tool args → `executor` returns an `is_error` result describing the schema violation; the model retries the call.

---

## 11. MCP server (shared schema)

`mcp_server.py` builds a low-level `mcp.server.lowlevel.Server` (not
`FastMCP` — deliberately, so the JSON Schema in `ToolSpec.input_schema` is
reused byte-for-byte instead of being re-derived from a Python function
signature, which would risk silently drifting from the schema the SDK loop
actually sends the model) and registers **the same `TOOLS` list** the SDK
loop uses. The sharing mechanism:

- `tools/schema.py::ToolSpec` is the single source of truth `{name, description, input_schema (JSON Schema), handler}`.
- `anthropic_tools()` renders `ToolSpec` → SDK tool dicts for the loop.
- `mcp_server.py`'s `@server.list_tools()` handler renders the same specs → MCP `types.Tool` objects directly (name/description/inputSchema straight from the `ToolSpec`, nothing re-derived).
- `@server.call_tool()` dispatches every call through the identical `ToolExecutor.run()` the SDK loop uses, so `ToolError`/`CS14ApiError` map to `isError: true` the same way regardless of which caller is driving.
- `tests/test_tools_schema.py` locks down the invariants both renderings depend on (unique names, strict schemas, every `required` key present in `properties`); `scripts/mcp_smoke.py` drives the actual MCP `initialize`/`tools/list` handshake end-to-end and asserts all 13 tools are listed with schemas.

The MCP server constructs its own `CS14Client` (creds from env, falling back to `dry_run` if the backend is unreachable or `CS14_MOCK=1` is set) and runs over stdio (`uv run survey-agent-mcp`). Result: any MCP-capable host (Claude Desktop, Claude Code, another agent) gets the exact same 13 tools, backed by the same handlers and the same backend, with zero duplicated schema.

---

## 12. Evaluation

**Case format** (`evals/cases/*.json`):
```json
{
  "name": "bilingual_ab_xhs",
  "instruction": "小红书风格，中英双语，A/B两组(一组显示点赞一组不显示)，两个帖子加一个5点李克特，发布给我链接",
  "mock_script": [ {"tool_use":[{"name":"create_survey","input":{...}}]}, ... {"final":"..."} ],
  "expected_sequence": ["create_survey","add_post","update_post_display","add_post","update_post_display","add_survey_question","publish_survey"],
  "terminal_assert": {
    "status": "published",
    "share_code": "*",
    "supported_languages_superset": ["en","zh-CN"],
    "num_groups": 2,
    "post_count": 2,
    "has_question_type": "likert"
  }
}
```

**Grader (two layers, `graders.py`):**
1. `sequence_match(actual_tool_names, expected_sequence)` — order-aware similarity (longest-common-subsequence ratio + required-tool presence). Tolerates extra `get_survey`/`list_*` reads; penalizes missing or out-of-order mutations (e.g. `publish` before `add_post`).
2. `terminal_state_assert(RunContext, terminal_assert)` — pulls the final survey via `get_survey` and checks status/languages/groups/post count/question types. Wildcards (`"*"`) assert presence only.

**Two run modes** (`run_evals.sh`):
- **mock (default, CI):** `MockModel` replays each case's script; deterministic tool sequence; real HTTP to a local backend (or `--dry-run` with stubbed handlers for no-backend CI). Asserts both layers.
- **real (`--real`):** `RealModel` with the instruction only (no script); measures how often the live model produces a passing sequence + terminal state. Reports pass rate, avg turns, avg cost from the trace.

Scorecard printed as a table: per-case seq-score, terminal-pass, turns, tokens, cost.

---

## 13. CLI & demo

```bash
# real model (needs ANTHROPIC_API_KEY or `ant auth login`)
uv run survey-agent "帮我建一个小红书风格的中英双语问卷，A/B两组，两个帖子加一个李克特量表，发布并给我链接" \
  --model claude-opus-4-8 --trace traces/run1.jsonl

# offline / CI — no key, model decisions replayed, real backend HTTP
uv run survey-agent "" --mock evals/cases/01_bilingual_ab_xhs.json --trace traces/mock1.jsonl

# point at a compatible endpoint / proxy
uv run survey-agent "..." --base-url https://my-proxy/v1 --model some-model

# MCP server (same tools)
uv run survey-agent-mcp

# evals
uv run python -m evals.runner            # mock, all cases
uv run python -m evals.runner --real     # live model
```

`scripts/demo.sh`: (1) `docker compose up -d db` + backend on host (or full `docker compose up -d`), (2) `python -m scripts.seed_client_demo` to get `cs14.demo@example.com`, (3) run three instructions (minimal EN single-group; bilingual A/B XHS; recovery-from-422), (4) print the three share links and the trace paths.

---

## 14. Open questions / future work

- Real public URLs for `add_post` OG fetch vs. always `update_post_display` — demo defaults to the latter to stay network-independent (SSRF block hits localhost anyway).
- Prompt caching the frozen system prefix once the tool list stabilizes (Opus 4.8, 5-min TTL) — deferred; current runs are short enough that write premium isn't worth it.
- Second toolset (analytics/export/close) behind `--advanced` once the core chain's tool-selection accuracy is measured on the eval suite.
