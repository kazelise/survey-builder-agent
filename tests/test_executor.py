"""ToolExecutor tests: arg validation, truncation, and exception -> is_error
mapping. No network — handlers are tiny stubs defined in this file."""

from __future__ import annotations

import json

from survey_agent.context import HandlerContext, RunContext
from survey_agent.executor import ToolExecutor
from survey_agent.http_client import CS14ApiError
from survey_agent.tools.schema import ToolError, ToolSpec


def _ctx() -> HandlerContext:
    return HandlerContext(client=None, run=RunContext())  # handlers below never touch .client


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
    "additionalProperties": False,
}


def _spec(name: str, handler) -> ToolSpec:
    return ToolSpec(name=name, description="test", input_schema=ECHO_SCHEMA, handler=handler)


def test_missing_required_field_is_rejected_before_handler_runs():
    called = {"n": 0}

    def handler(ctx, args):
        called["n"] += 1
        return {"ok": True}

    executor = ToolExecutor([_spec("echo", handler)], _ctx())
    result = executor.run("tu_1", "echo", {})
    assert result.is_error is True
    assert "missing required fields" in result.content
    assert called["n"] == 0  # never reached the handler


def test_unknown_field_is_rejected():
    executor = ToolExecutor([_spec("echo", lambda ctx, args: {"ok": True})], _ctx())
    result = executor.run("tu_1", "echo", {"x": 1, "surprise": True})
    assert result.is_error is True
    assert "unknown fields" in result.content


def test_unknown_tool_name_is_reported_as_error_not_raised():
    executor = ToolExecutor([], _ctx())
    result = executor.run("tu_1", "does_not_exist", {})
    assert result.is_error is True
    assert "Unknown tool" in result.content


def test_tool_error_from_handler_becomes_is_error_result():
    def handler(ctx, args):
        raise ToolError("locked field")

    executor = ToolExecutor([_spec("echo", handler)], _ctx())
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is True
    assert result.content == "locked field"


def test_cs14_api_error_from_handler_becomes_is_error_result_with_status():
    def handler(ctx, args):
        raise CS14ApiError(409, {"detail": "cannot change published survey"})

    executor = ToolExecutor([_spec("echo", handler)], _ctx())
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is True
    body = json.loads(result.content)
    assert body["status"] == 409


def test_crashing_handler_never_orphans_the_tool_use_id():
    def handler(ctx, args):
        raise RuntimeError("boom")

    executor = ToolExecutor([_spec("echo", handler)], _ctx())
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.tool_use_id == "tu_1"
    assert result.is_error is True
    assert "boom" in result.content


def test_oversized_list_result_is_summarized_not_hard_truncated_mid_json():
    def handler(ctx, args):
        return list(range(2000))  # long enough to exceed a small max_chars

    executor = ToolExecutor([_spec("echo", handler)], _ctx(), result_max_chars=100)
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is False
    parsed = json.loads(result.content)
    assert parsed["truncated"] is True
    assert parsed["count"] == 2000
    assert len(parsed["first_n"]) == 10


def test_oversized_items_with_large_elements_still_produces_valid_json():
    # Regression test: when the first-10-items summary itself still exceeds
    # result_max_chars (items are large objects, not small ints), the old
    # code did `json.dumps(summary)[:max_chars]`, slicing mid-string and
    # returning invalid JSON as a "successful" (is_error=False) tool_result.
    # The fix must shrink the item count until the summary actually fits.
    def handler(ctx, args):
        return {"items": [{"id": i, "blob": "x" * 500} for i in range(20)]}

    executor = ToolExecutor([_spec("echo", handler)], _ctx(), result_max_chars=4000)
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is False
    assert len(result.content) <= 4000
    parsed = json.loads(result.content)  # must not raise json.JSONDecodeError
    assert parsed["truncated"] is True
    assert 0 <= len(parsed["items"]) < 20


def test_oversized_posts_with_large_elements_still_produces_valid_json():
    # Same bug, "posts" envelope branch (list_posts-shaped results).
    def handler(ctx, args):
        return {"count": 20, "posts": [{"id": i, "blob": "x" * 500} for i in range(20)]}

    executor = ToolExecutor([_spec("echo", handler)], _ctx(), result_max_chars=4000)
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is False
    assert len(result.content) <= 4000
    parsed = json.loads(result.content)  # must not raise json.JSONDecodeError
    assert parsed["truncated"] is True
    assert 0 <= len(parsed["posts"]) < 20


def test_pathologically_tiny_budget_still_yields_valid_json_or_empty_string():
    # Even when even an empty-summary shape can't fit, _truncate must never
    # return a char-sliced, invalid-JSON fragment.
    def handler(ctx, args):
        return {"items": [{"id": i, "blob": "x" * 500} for i in range(20)]}

    executor = ToolExecutor([_spec("echo", handler)], _ctx(), result_max_chars=5)
    result = executor.run("tu_1", "echo", {"x": 1})
    assert result.is_error is False
    if result.content:
        json.loads(result.content)  # must not raise if non-empty
