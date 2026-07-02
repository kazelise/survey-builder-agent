"""Turns a case's declarative, pure-JSON ``mock_script`` into the list of
callables ``MockModel`` expects (``survey_agent.model.MockModel``).

Why this exists: a build chain assigns ids dynamically (the dry-run backend
hands out ``survey_id``/``post_id``/``question_id`` sequentially per run), so
a case file can't hardcode them. ``cli.py``'s built-in demo script solves
this with hand-written Python closures; this module generalizes the same
trick so every eval case can stay a flat, diffable, non-executable JSON file
(DESIGN.md §12's case format) instead of a Python fixture.

Two features on top of a plain ``{"tool_use": [...]}"`` / ``{"final": "..."}``
step:

1. ``"$name"`` / ``"$name[idx]"`` string values anywhere in a step's tool
   ``input`` are resolved against a running ``captured`` dict built from the
   JSON payload of every prior tool_result (see ``_update_captured``).
2. ``{"if_last_error": {...}, "else": {...}}`` steps branch on whether the
   immediately preceding turn's tool_result(s) contained an error — this is
   what lets a case script exercise the 422/409 recovery path
   deterministically (DESIGN.md §8's "conditional branches").
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)(\[(-?\d+)\])?$")


def build_mock_script(steps: list[dict]) -> list[Callable[[list[dict]], dict]]:
    """``steps`` is the case's ``mock_script`` field, straight from JSON.

    Returns a list of ``(last_tool_results) -> entry`` callables — the exact
    shape ``MockModel`` accepts (see ``model.py``'s docstring). One shared
    ``captured`` dict is closed over by every callable so state accumulates
    across the whole run.
    """
    captured: dict[str, Any] = {}
    return [_make_step(step, captured) for step in steps]


def _make_step(step_def: dict, captured: dict[str, Any]) -> Callable[[list[dict]], dict]:
    def step(last_results: list[dict]) -> dict:
        had_error = False
        for block in last_results:
            is_err = bool(block.get("is_error"))
            had_error = had_error or is_err
            if is_err:
                continue
            data = _parse_content(block.get("content"))
            if isinstance(data, dict):
                _update_captured(captured, data)
        captured["_last_error"] = had_error

        branch = step_def
        if "if_last_error" in step_def:
            branch = step_def["if_last_error"] if had_error else step_def.get("else", {"final": "(no else branch)"})

        if "final" in branch:
            text = branch["final"]
            try:
                text = text.format(**captured)
            except (KeyError, IndexError):
                pass  # missing placeholder -> leave the template text as-is
            return {"final": text}

        return {
            "tool_use": [
                {"name": call["name"], "input": _resolve(call.get("input", {}), captured)}
                for call in branch["tool_use"]
            ]
        }

    return step


def _parse_content(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


# Result shapes are distinguished by their sibling keys since several tools
# share a key name (e.g. both add_post and update_post_display return
# "post_id") — see tools/content.py for the exact shapes being matched here.
def _update_captured(captured: dict[str, Any], data: dict) -> None:
    for key, value in data.items():
        captured[key] = value
    if "post_id" in data and "og_metadata_fetched" in data:  # add_post only
        captured.setdefault("post_ids", []).append(data["post_id"])
    if "question_id" in data and "question_type" in data:  # add_*_question
        captured.setdefault("question_ids", []).append(data["question_id"])
        captured.setdefault("question_types", []).append(data["question_type"])
    if "comment_id" in data:
        captured.setdefault("comment_ids", []).append(data["comment_id"])


def _resolve(value: Any, captured: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = _REF_RE.match(value)
        if not m:
            return value
        name, _, idx = m.groups()
        target = captured.get(name)
        if idx is not None and isinstance(target, list):
            try:
                return target[int(idx)]
            except IndexError:
                return None
        return target
    if isinstance(value, dict):
        return {k: _resolve(v, captured) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, captured) for v in value]
    return value
