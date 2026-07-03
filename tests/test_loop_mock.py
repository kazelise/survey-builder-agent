"""loop.run() driven end-to-end by MockModel. Uses CS14Client(dry_run=True)
so the tool layer still runs (per DESIGN.md §8: mock mode fakes the model's
decisions, not the tool layer) but with zero real network."""

from __future__ import annotations

from survey_agent.config import Settings
from survey_agent.context import HandlerContext, RunContext
from survey_agent.executor import ToolExecutor
from survey_agent.http_client import CS14Client
from survey_agent.loop import run, trim_context
from survey_agent.model import MockModel, ModelResponse, Usage
from survey_agent.tools import TOOLS
from survey_agent.tools.schema import anthropic_tools
from survey_agent.trace import Tracer


def _build(script):
    client = CS14Client(base_url="http://unused", dry_run=True)
    run_ctx = RunContext()
    ctx = HandlerContext(client=client, run=run_ctx)
    executor = ToolExecutor(TOOLS, ctx, result_max_chars=4000)
    settings = Settings(max_turns=10)
    tracer = Tracer(None)  # no file — just exercises the code path
    model = MockModel(script)
    return model, executor, ctx, settings, tracer


def test_loop_reaches_published_terminal_state():
    script = [
        {"tool_use": [{"name": "create_survey", "input": {
            "title": "T", "default_language": "en", "supported_languages": ["en"],
        }}]},
        {"tool_use": [{"name": "add_post", "input": {"survey_id": 1, "original_url": "https://x.com/1", "order": 1}}]},
        {"tool_use": [{"name": "publish_survey", "input": {"survey_id": 1}}]},
        {"final": "Done. Share link: /survey/dry0001?lang=en"},
    ]
    model, executor, ctx, settings, tracer = _build(script)
    result = run("build me a survey", "system", model, anthropic_tools(TOOLS), executor, ctx, settings, tracer)

    assert result.reason == "done"
    assert result.state["status"] == "published"
    assert result.state["survey_id"] == 1
    assert result.state["post_ids"] == [1]
    assert "Done" in result.final_text


def test_loop_stops_at_max_turns_if_model_never_finishes():
    # A script with only tool_use turns and no "final" — the loop must not
    # hang or crash, it should hit max_turns and return cleanly.
    script = [{"tool_use": [{"name": "list_surveys", "input": {}}]}] * 3
    model, executor, ctx, settings, tracer = _build(script)
    settings.max_turns = 3

    result = run("noop", "system", model, anthropic_tools(TOOLS), executor, ctx, settings, tracer)

    assert result.reason == "max_turns"
    assert result.final_text is None
    assert result.turns == 3


def test_failed_tool_call_still_produces_a_tool_result_block():
    # publish_survey on a survey with no posts is a local ToolError
    # (executor catches it) — the loop must keep the run alive and not crash
    # on the is_error result, and every tool_use id must get a tool_result.
    script = [
        {"tool_use": [{"name": "create_survey", "input": {
            "title": "T", "default_language": "en", "supported_languages": ["en"],
        }}]},
        {"tool_use": [{"name": "publish_survey", "input": {"survey_id": 1}}]},  # no posts yet -> is_error
        {"final": "gave up after error"},
    ]
    model, executor, ctx, settings, tracer = _build(script)
    result = run("x", "system", model, anthropic_tools(TOOLS), executor, ctx, settings, tracer)

    assert result.reason == "done"
    assert result.state["status"] == "draft"  # publish never actually succeeded
    assert result.final_text == "gave up after error"


class _FixedStopReasonModel:
    """Minimal Model double that always returns one fixed ModelResponse.

    MockModel's script format (model.py) can only ever produce
    stop_reason "tool_use" or "end_turn" — it has no way to synthesize
    "max_tokens"/"stop_sequence"/other real-API stop reasons, so loop.py's
    handling of those branches would otherwise be entirely untested (see
    the "any non-tool_use, non-refusal stop_reason is silently treated as
    done" finding). This stub fills that gap without needing a real
    Anthropic call.
    """

    def __init__(self, stop_reason: str, text: str = "partial answer"):
        self._stop_reason = stop_reason
        self._text = text

    def complete(self, system, messages, tools):
        return ModelResponse(
            text=self._text,
            tool_uses=[],
            stop_reason=self._stop_reason,
            raw_content=[{"type": "text", "text": self._text}],
            usage=Usage(),
        )


def _run_with_stub(stop_reason: str, text: str = "partial answer"):
    client = CS14Client(base_url="http://unused", dry_run=True)
    run_ctx = RunContext()
    ctx = HandlerContext(client=client, run=run_ctx)
    executor = ToolExecutor(TOOLS, ctx, result_max_chars=4000)
    settings = Settings(max_turns=5)
    tracer = Tracer(None)
    model = _FixedStopReasonModel(stop_reason, text)
    return run("x", "system", model, anthropic_tools(TOOLS), executor, ctx, settings, tracer)


def test_max_tokens_stop_reason_is_not_silently_treated_as_done():
    # Regression: loop.py used to fold EVERY non-tool_use, non-refusal
    # stop_reason into the generic "done" branch. A response truncated by
    # max_tokens (e.g. cut off mid a large JSON tool_use payload) is not a
    # trustworthy, complete final answer and must be distinguishable from
    # a real "done".
    result = _run_with_stub("max_tokens")
    assert result.reason == "max_tokens"
    assert result.reason != "done"


def test_unrecognized_stop_reason_is_also_not_treated_as_done():
    # A future/unexpected stop_reason (e.g. "pause_turn") must not be
    # silently swallowed into "done" either.
    result = _run_with_stub("pause_turn")
    assert result.reason == "pause_turn"
    assert result.reason != "done"


def test_end_turn_and_stop_sequence_are_still_treated_as_a_clean_done_answer():
    # Sanity check the fix doesn't over-correct: genuinely complete
    # responses (end_turn, or a custom stop_sequence hit) are still "done".
    for stop_reason in ("end_turn", "stop_sequence"):
        result = _run_with_stub(stop_reason, text="Done. Share link: /survey/x?lang=en")
        assert result.reason == "done"
        assert result.final_text == "Done. Share link: /survey/x?lang=en"


def test_refusal_stop_reason_terminates_the_run_with_no_final_text():
    # Regression: loop.py's stop_reason=="refusal" branch was previously
    # exercised by neither the mock suite nor the eval suite -- MockModel
    # had no way to synthesize it (only "tool_use"/"end_turn"), and the
    # eval suite's "refuse_*" cases have the model refuse via ordinary text
    # (stop_reason="end_turn"), which goes through the "done" branch
    # instead. Now that MockModel can produce a {"refusal": ...} entry,
    # pin the branch's actual behavior: no final_text (by design, even
    # though the API response carries refusal text), reason="refusal".
    script = [{"refusal": "I can't help with that."}]
    model, executor, ctx, settings, tracer = _build(script)
    result = run(
        "do something out of scope", "system", model, anthropic_tools(TOOLS), executor, ctx, settings, tracer
    )

    assert result.reason == "refusal"
    assert result.final_text is None
    assert result.turns == 1
    assert result.state["status"] is None  # no survey was ever built


def test_trim_context_keeps_first_message_and_recent_rounds():
    head = {"role": "user", "content": "original instruction"}
    # Build 10 fake (assistant, user) round pairs, each with a long content
    # string so the char/4 budget estimate is exceeded.
    rounds = []
    for i in range(10):
        rounds.append({"role": "assistant", "content": "x" * 500})
        rounds.append({"role": "user", "content": "y" * 500})
    messages = [head, *rounds]

    trim_context(messages, budget_tokens=100, keep_recent_rounds=2)

    assert messages[0] == head
    # Only the most recent 2 round-pairs (4 messages) should remain after head.
    assert len(messages) == 1 + 2 * 2
    assert messages[-1]["content"] == "y" * 500
