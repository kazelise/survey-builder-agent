"""The single ordered list of tools the model can call.

`TOOLS` is the shared schema source referenced throughout DESIGN.md: the SDK
loop renders it via `schema.anthropic_tools()`, and `mcp_server.py` iterates
this same list to register each tool against the MCP `Server` API — no tool
gets defined twice.
"""

from __future__ import annotations

from . import content, handbook, survey
from .schema import ToolSpec

TOOLS: list[ToolSpec] = [*survey.TOOLS, *content.TOOLS, *handbook.TOOLS]

__all__ = ["TOOLS"]
