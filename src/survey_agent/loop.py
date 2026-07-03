"""The agent loop. Explicit control flow, no framework — DESIGN.md §6.

`run()` only depends on the `Model` protocol and `ToolExecutor`, never on
`anthropic` or `httpx` directly. That is what makes a full run exercisable
under `MockModel` with zero network and zero API key
(tests/test_loop_mock.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .config import Settings, estimate_cost
from .context import HandlerContext
from .executor import ToolExecutor
from .model import Model, ModelUnavailableError
from .trace import Tracer

PUBLISHED_REMINDER = (
    "[system reminder] publish_survey succeeded — the build chain is done. "
    "Reply now with the share link and a one-line summary; do not call more tools."
)

# Anthropic's Messages API only accepts "user"/"assistant" roles in
# `messages` (system is a top-level param, not a mid-conversation role), so
# the "role: system" reminder in DESIGN.md §6.2 is implemented as an extra
# text block appended to the tool_result turn instead of a literal message
# with role="system".
ProgressCallback = Callable[[str, dict], None]


@dataclass
class RunResult:
    final_text: str | None
    state: dict
    turns: int
    # "done" | "max_turns" | "model_unavailable" | "refusal" | the raw
    # Anthropic `stop_reason` for anything else non-terminal-looking (e.g.
    # "max_tokens", "pause_turn") — see _DONE_STOP_REASONS below.
    reason: str


# stop_reason values that mean the model chose to stop with a complete,
# trustworthy final answer. Anything else that isn't "tool_use"/"refusal"
# (most notably "max_tokens" — the response was cut off mid-generation,
# e.g. while emitting a large tool_use/JSON payload) must NOT be folded
# into "done": resp.text may be empty or truncated, and callers (cli.py's
# exit code, evals) need to be able to tell a genuine completion apart
# from an incomplete one.
_DONE_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})


def run(
    instruction: str,
    system_prompt: str,
    model: Model,
    tools: list[dict],
    executor: ToolExecutor,
    ctx: HandlerContext,
    settings: Settings,
    tracer: Tracer,
    on_event: ProgressCallback | None = None,
    fallback_model: Model | None = None,
) -> RunResult:
    def emit(kind: str, payload: dict) -> None:
        if on_event is not None:
            on_event(kind, payload)

    messages: list[dict] = [{"role": "user", "content": instruction}]
    active_model = model
    published_hinted = False

    for turn in range(settings.max_turns):
        trim_context(messages, settings.context_budget_tokens)

        start = time.monotonic()
        resp, active_model = _complete_with_fallback(
            active_model, model, fallback_model, system_prompt, messages, tools, emit, turn
        )
        if resp is None:
            tracer.run_summary(reason="model_unavailable", turns=turn, **ctx.run.as_state())
            return RunResult(None, ctx.run.as_state(), turn, "model_unavailable")
        latency_ms = (time.monotonic() - start) * 1000

        usage = {
            "input": resp.usage.input_tokens,
            "output": resp.usage.output_tokens,
            "cache_read": resp.usage.cache_read_tokens,
            "cache_creation": resp.usage.cache_creation_tokens,
        }
        price = settings.price_for(active_model.model_id)
        cost = estimate_cost(usage, price)
        tracer.round(turn, len(messages), len(tools), usage, latency_ms, cost)
        emit("round", {"turn": turn, "usage": usage, "latency_ms": latency_ms})

        # Preserve tool_use blocks verbatim — the next request 400s if any
        # tool_use id from this turn doesn't get a matching tool_result.
        messages.append({"role": "assistant", "content": resp.raw_content})

        if resp.stop_reason == "refusal":
            tracer.run_summary(reason="refusal", turns=turn + 1, **ctx.run.as_state())
            return RunResult(None, ctx.run.as_state(), turn + 1, "refusal")

        if resp.stop_reason != "tool_use":
            reason = "done" if resp.stop_reason in _DONE_STOP_REASONS else resp.stop_reason
            tracer.run_summary(reason=reason, turns=turn + 1, final_text=resp.text, **ctx.run.as_state())
            return RunResult(resp.text, ctx.run.as_state(), turn + 1, reason)

        # ALL tool_result blocks go in ONE user message — splitting them
        # across messages trains the model to stop parallelizing tool calls.
        tool_result_blocks: list[dict] = []
        for tool_use in resp.tool_uses:
            call_start = time.monotonic()
            result = executor.run(tool_use.id, tool_use.name, tool_use.input)
            duration_ms = (time.monotonic() - call_start) * 1000
            tracer.tool_call(tool_use.name, tool_use.input, result.content, result.is_error, duration_ms)
            emit(
                "tool_call",
                {
                    "name": tool_use.name,
                    "input": tool_use.input,
                    "is_error": result.is_error,
                    "result": result.content,
                },
            )
            # A failed tool still produces a tool_result (is_error=true),
            # never a dropped block — see the module docstring above.
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": result.tool_use_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )

        if not published_hinted and ctx.run.status == "published":
            tool_result_blocks.append({"type": "text", "text": PUBLISHED_REMINDER})
            published_hinted = True

        messages.append({"role": "user", "content": tool_result_blocks})

    tracer.run_summary(reason="max_turns", turns=settings.max_turns, **ctx.run.as_state())
    return RunResult(None, ctx.run.as_state(), settings.max_turns, "max_turns")


def _complete_with_fallback(
    active_model: Model,
    primary_model: Model,
    fallback_model: Model | None,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    emit: Callable[[str, dict], None],
    turn: int,
):
    """Try `active_model`; on ModelUnavailableError, switch to `fallback_model`
    once (and stay switched for the rest of the run — cache is cold on the
    fallback but the run completes, per DESIGN.md §10)."""
    try:
        return active_model.complete(system_prompt, messages, tools), active_model
    except ModelUnavailableError:
        if active_model is primary_model and fallback_model is not None:
            emit("model_fallback", {"turn": turn})
            try:
                return fallback_model.complete(system_prompt, messages, tools), fallback_model
            except ModelUnavailableError:
                return None, active_model
        return None, active_model


def trim_context(messages: list[dict], budget_tokens: int, keep_recent_rounds: int = 6) -> None:
    """Budget-based trim, run before every `complete()` call.

    Once the (chars/4) token estimate exceeds `budget_tokens`, drop the
    oldest complete (assistant tool_use, user tool_result) round pairs —
    never a lone half-pair, or the next request would 400 on an orphaned
    tool_use id. Always keeps `messages[0]` (the original instruction) and
    the most recent `keep_recent_rounds` rounds. Safe to drop old rounds
    because survey_id/share_code live in RunContext, not only in the
    transcript (context.py).
    """

    def estimate_tokens() -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // 4

    if estimate_tokens() <= budget_tokens:
        return

    head, rounds = messages[0], messages[1:]
    round_pairs = [rounds[i : i + 2] for i in range(0, len(rounds), 2)]
    while len(round_pairs) > keep_recent_rounds and estimate_tokens() > budget_tokens:
        round_pairs.pop(0)
        messages[:] = [head, *[m for pair in round_pairs for m in pair]]
