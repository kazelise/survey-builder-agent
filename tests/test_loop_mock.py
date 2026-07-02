"""loop.run() driven end-to-end by MockModel. Uses CS14Client(dry_run=True)
so the tool layer still runs (per DESIGN.md §8: mock mode fakes the model's
decisions, not the tool layer) but with zero real network."""

from __future__ import annotations

from survey_agent.config import Settings
from survey_agent.context import HandlerContext, RunContext
from survey_agent.executor import ToolExecutor
from survey_agent.http_client import CS14Client
from survey_agent.loop import run, trim_context
from survey_agent.model import MockModel
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
