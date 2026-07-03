"""Single source of truth for tool definitions.

``ToolSpec`` bundles a JSON Schema input contract with the handler that
executes it. ``anthropic_tools()`` renders the list for the SDK loop; the
MCP server (``mcp_server.py``) iterates the same ``TOOLS`` list (see
``tools/__init__.py``) and registers each spec's name/description/
input_schema directly against the low-level MCP ``Server`` API — nothing
here gets redefined or re-derived by either caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# (HandlerContext, args) -> JSON-serializable payload. Handlers raise
# ToolError for validation failures they catch themselves, or let
# CS14ApiError/other exceptions propagate — the executor maps both to an
# is_error tool_result (see executor.py).
Handler = Callable[[Any, dict], dict]


class ToolError(Exception):
    """A handler-detected problem that should go back to the model as
    ``is_error`` without ever reaching the network (e.g. a locked field, an
    empty options list). Distinct from CS14ApiError, which is a real HTTP
    response the backend sent back."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler


def anthropic_tools(specs: list[ToolSpec]) -> list[dict]:
    """Render ToolSpecs as the `tools=[...]` list the Anthropic SDK expects."""
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in specs
    ]
