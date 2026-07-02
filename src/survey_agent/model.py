"""Model abstraction: same output shape whether decisions come from a live
Anthropic call or a scripted replay.

`loop.py` only ever talks to the `Model` protocol, never to `anthropic` or a
mock lookalike directly — that's what lets `--mock` runs execute the exact
same loop.py code path as a real run (DESIGN.md §4, §8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class ModelResponse:
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str
    # Appended to `messages` verbatim by the loop so tool_use blocks survive
    # the round trip untouched — see loop.py's invariant list.
    raw_content: list[dict]
    usage: Usage = field(default_factory=Usage)


class Model(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


class ModelUnavailableError(RuntimeError):
    """Raised when a real model call fails after SDK-level retries are
    exhausted (rate limit, 5xx, refusal never reaching content[0]). loop.py
    catches this to drive the fallback-model path (DESIGN.md §10)."""


class RealModel:
    """Wraps `anthropic.Anthropic`. `base_url`/`model` are injected from
    Settings, so pointing at an OpenAI-compatible proxy later is a config
    change, not a code change (DESIGN.md §4).

    Note: DESIGN.md's `thinking={"type":"adaptive"}` / `output_config`
    knobs describe a not-yet-real model generation. This implementation
    intentionally sticks to the stable, documented Messages API surface
    (model/max_tokens/system/messages/tools/tool_choice) so it's verifiable
    today; adding extended-thinking config is a one-line change here once
    there's a live key to test it against.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8000,
    ):
        import anthropic  # imported lazily: --mock runs must work without the SDK reaching out anywhere

        self._model = model
        self._max_tokens = max_tokens
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._anthropic = anthropic

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
            )
        except (
            self._anthropic.RateLimitError,
            self._anthropic.APIStatusError,
            self._anthropic.APIConnectionError,
        ) as exc:
            raise ModelUnavailableError(str(exc)) from exc

        raw_content = [block.model_dump() for block in resp.content]
        text_parts = [b["text"] for b in raw_content if b.get("type") == "text"]
        tool_uses = [
            ToolUse(id=b["id"], name=b["name"], input=b.get("input") or {})
            for b in raw_content
            if b.get("type") == "tool_use"
        ]
        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return ModelResponse(
            text="\n".join(text_parts),
            tool_uses=tool_uses,
            stop_reason=resp.stop_reason or "end_turn",
            raw_content=raw_content,
            usage=usage,
        )


class MockModel:
    """Replays a scripted list of decisions instead of calling the API.

    Each script entry is one of:
      - {"tool_use": [{"name": ..., "input": {...}}, ...]}
      - {"final": "text"}
      - a callable `(last_tool_results) -> entry` for conditional branches
        (DESIGN.md §8): it receives the tool_result blocks from the most
        recent user message, so a script can react to e.g. `add_post`
        returning `og_metadata_fetched: false` without any extra plumbing
        from the loop.
    """

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self._pos = 0
        self._next_id = 0

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        if self._pos >= len(self._script):
            # Script exhausted without an explicit "final": end the run
            # cleanly rather than raising, so a short script is still a
            # valid (if incomplete) fixture.
            text = "(mock script exhausted)"
            return ModelResponse(
                text=text, tool_uses=[], stop_reason="end_turn", raw_content=[{"type": "text", "text": text}]
            )

        entry = self._script[self._pos]
        self._pos += 1
        if callable(entry):
            entry = entry(_last_tool_results(messages))

        if "final" in entry:
            text = entry["final"]
            return ModelResponse(
                text=text, tool_uses=[], stop_reason="end_turn", raw_content=[{"type": "text", "text": text}]
            )

        raw_content: list[dict] = []
        tool_uses: list[ToolUse] = []
        for call in entry["tool_use"]:
            self._next_id += 1
            tool_use_id = call.get("id") or f"mock_{self._next_id}"
            block = {"type": "tool_use", "id": tool_use_id, "name": call["name"], "input": call["input"]}
            raw_content.append(block)
            tool_uses.append(ToolUse(id=tool_use_id, name=call["name"], input=call["input"]))
        return ModelResponse(text="", tool_uses=tool_uses, stop_reason="tool_use", raw_content=raw_content)


def _last_tool_results(messages: list[dict]) -> list[dict]:
    """Pull the tool_result blocks out of the most recent user message, if any."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            blocks = [
                b for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if blocks:
                return blocks
    return []
