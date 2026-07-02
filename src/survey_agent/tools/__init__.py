"""The single ordered list of tools the model can call.

`TOOLS` is the shared schema source referenced throughout DESIGN.md: the SDK
loop renders it via `schema.anthropic_tools()`, and a future MCP server would
iterate this same list to register each handler under `@mcp.tool()` — no
tool gets defined twice.
"""

from __future__ import annotations

from . import content, survey
from .schema import ToolSpec

TOOLS: list[ToolSpec] = [*survey.TOOLS, *content.TOOLS]

__all__ = ["TOOLS"]
