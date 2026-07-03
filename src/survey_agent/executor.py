"""ToolExecutor: name -> handler dispatch, light arg validation, result
truncation, and exception -> is_error tool_result mapping.

This is the only place that knows about the Anthropic tool_result wire
format, which is what keeps handlers pure-ish (`(HandlerContext, args) ->
dict` in, JSON out) and reusable outside this loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .context import HandlerContext
from .http_client import CS14ApiError
from .tools.schema import ToolError, ToolSpec


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


class ToolExecutor:
    def __init__(self, tools: list[ToolSpec], ctx: HandlerContext, result_max_chars: int = 4000):
        self._by_name = {t.name: t for t in tools}
        self._ctx = ctx
        self._max_chars = result_max_chars

    def run(self, tool_use_id: str, name: str, args: dict) -> ToolResult:
        spec = self._by_name.get(name)
        if spec is None:
            return ToolResult(tool_use_id, f"Unknown tool: {name!r}", is_error=True)

        error = _validate_args(spec.input_schema, args)
        if error:
            return ToolResult(tool_use_id, f"Invalid arguments for {name}: {error}", is_error=True)

        try:
            payload = spec.handler(self._ctx, args)
        except ToolError as exc:
            return ToolResult(tool_use_id, str(exc), is_error=True)
        except CS14ApiError as exc:
            body = json.dumps({"status": exc.status, "body": exc.body}, default=str, ensure_ascii=False)
            return ToolResult(tool_use_id, body, is_error=True)
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            # A crashing handler must still produce a tool_result, or the
            # next request 400s on an orphaned tool_use id (loop.py's
            # central invariant, DESIGN.md §6.1).
            return ToolResult(tool_use_id, f"{type(exc).__name__}: {exc}", is_error=True)

        return ToolResult(tool_use_id, self._truncate(payload), is_error=False)

    def _truncate(self, payload: object) -> str:
        text = json.dumps(payload, default=str, ensure_ascii=False)
        if len(text) <= self._max_chars:
            return text

        # Oversized results get summarized rather than hard-cut mid-JSON, so
        # the model still sees a valid, useful shape (DESIGN.md §10). The
        # summary is re-validated against the budget (not just sliced) —
        # see _fit_summary.
        if isinstance(payload, list):
            return self._fit_summary(lambda n: {"count": len(payload), "first_n": payload[:n], "truncated": True})
        if isinstance(payload, dict) and isinstance(payload.get("posts"), list):
            posts = payload["posts"]
            return self._fit_summary(lambda n: {**payload, "posts": posts[:n], "truncated": True})
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
            return self._fit_summary(lambda n: {**payload, "items": items[:n], "truncated": True})
        return text[: self._max_chars] + "...[truncated]"

    def _fit_summary(self, build: Callable[[int], dict]) -> str:
        """Build a `{..., truncated: True}` summary via `build(n)` for a
        shrinking item count until the *serialized* result actually fits
        `self._max_chars`, so the caller never receives a char-sliced,
        invalid-JSON fragment — the bug this replaces: dumping a fixed
        first-10-items summary then blindly `[:max_chars]`-slicing it,
        which produces truncated mid-string JSON whenever the first 10
        items are themselves large (see tests/test_executor.py)."""
        for n in (10, 5, 2, 1, 0):
            candidate = json.dumps(build(n), default=str, ensure_ascii=False)
            if len(candidate) <= self._max_chars:
                return candidate
        # Even an empty-item summary didn't fit — huge unrelated envelope
        # fields, or a pathologically small max_chars. Fall back to
        # ever-smaller fixed shapes rather than ever emitting invalid JSON.
        for fallback_obj in ({"truncated": True, "note": "result too large to summarize"}, {"truncated": True}, {}):
            fallback = json.dumps(fallback_obj, ensure_ascii=False)
            if len(fallback) <= self._max_chars:
                return fallback
        return ""  # max_chars smaller than "{}" — nothing valid can fit


def _validate_args(schema: dict, args: dict) -> str | None:
    """Minimal required/unknown-key check. Not a full JSON Schema validator
    (types, enums, min/max aren't checked here) — deliberately: those are
    cheap for the backend to reject with a precise 422, while a missing
    required field or a hallucinated field name is worth catching before
    spending a network round trip."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return f"missing required fields: {missing}"
    if schema.get("additionalProperties") is False:
        unknown = [k for k in args if k not in props]
        if unknown:
            return f"unknown fields: {unknown}"
    return None
