"""MCP server exposing the exact same 12 survey-builder tools as the SDK
loop, over stdio, so any MCP host (Claude Desktop, Claude Code, etc.) can
drive cs14 surveys directly.

This module is a pure adapter: it never redefines a tool's name,
description, or input_schema. It iterates ``tools.TOOLS`` (the single
schema source described in ``tools/__init__.py`` and ``tools/schema.py``)
and registers each ``ToolSpec`` as an MCP tool, dispatching calls through
the same ``ToolExecutor`` the CLI loop uses (executor.py) — so a call made
via MCP and a call made via the Anthropic tool-calling loop hit the exact
same handler, the exact same ``RunContext`` bookkeeping, and the exact same
error mapping (ToolError / CS14ApiError -> is_error).

We use the low-level ``mcp.server.lowlevel.Server`` API (not
``FastMCP``'s function-signature inference) specifically so the JSON Schema
in ``ToolSpec.input_schema`` is reused byte-for-byte instead of being
re-derived from a Python function signature, which would risk silently
drifting from the schema the SDK loop actually sends the model.

Auth: on startup we try ``ensure_researcher`` against CS14_BASE_URL. If
that fails (no backend reachable) — or if CS14_MOCK=1 is set — the server
falls back to ``CS14Client(dry_run=True)``, so `tools/list` and even
`tools/call` still work end-to-end with zero external dependencies. This
is the offline story for environments with no ANTHROPIC_API_KEY and no
cs14 backend running (see README-MCP.md).
"""

from __future__ import annotations

import asyncio
import os
import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .config import Settings
from .context import HandlerContext, RunContext
from .executor import ToolExecutor
from .http_client import CS14Client
from .tools import TOOLS
from .tools.auth import ensure_researcher

SERVER_NAME = "survey-agent"
SERVER_VERSION = "0.1.0"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_executor(settings: Settings | None = None, *, force_mock: bool | None = None) -> ToolExecutor:
    """Build the shared ToolExecutor + HandlerContext once per server
    process. Mirrors cli.py's wiring (Settings -> CS14Client -> auth ->
    RunContext -> ToolExecutor) but degrades to dry_run instead of exiting
    when the backend or credentials aren't usable, since an MCP server has
    no CLI exit code to hand back to a human."""
    settings = settings or Settings.from_env()
    mock = force_mock if force_mock is not None else _truthy(os.environ.get("CS14_MOCK"))

    client = CS14Client(
        base_url=settings.cs14_base_url,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        retry_max_delay=settings.retry_max_delay,
        dry_run=mock,
    )
    if not mock:
        try:
            ensure_researcher(client, settings.cs14_email, settings.cs14_password)
        except Exception as exc:  # noqa: BLE001 - fall back to mock rather than crash the server
            print(
                f"[survey-agent-mcp] cs14 backend at {settings.cs14_base_url} unreachable "
                f"({type(exc).__name__}: {exc}); falling back to dry_run/mock mode.",
                file=sys.stderr,
            )
            client.close()
            client = CS14Client(
                base_url=settings.cs14_base_url,
                max_retries=settings.max_retries,
                retry_base_delay=settings.retry_base_delay,
                retry_max_delay=settings.retry_max_delay,
                dry_run=True,
            )

    handler_ctx = HandlerContext(client=client, run=RunContext())
    return ToolExecutor(TOOLS, handler_ctx, result_max_chars=settings.tool_result_max_chars)


def build_server(executor: ToolExecutor) -> Server:
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)
    by_name = {spec.name: spec for spec in TOOLS}

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=spec.name, description=spec.description, inputSchema=spec.input_schema)
            for spec in TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        if name not in by_name:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name!r}")], isError=True
            )
        # tool_use_id is an Anthropic-loop concept the executor's ToolResult
        # carries around; MCP has no equivalent, so a constant placeholder
        # is fine — callers only care about `.content` / `.is_error` here.
        result = executor.run("mcp", name, arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.content)], isError=result.is_error
        )

    return server


async def run_stdio(force_mock: bool | None = None) -> None:
    executor = build_executor(force_mock=force_mock)
    server = build_server(executor)
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
