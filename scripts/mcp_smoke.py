#!/usr/bin/env python3
"""Minimal smoke test for the MCP server: spawn it over stdio, initialize,
list tools, and assert every tool in `tools.TOOLS` shows up with a non-empty
inputSchema. Exits non-zero on any mismatch.

Runs fully offline: CS14_MOCK=1 forces the server into dry_run mode so no
cs14 backend needs to be up and no ANTHROPIC_API_KEY is needed (the MCP
server never calls an LLM itself — that's the host's job).

Usage:
    uv run python scripts/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT / "src"))

from survey_agent.tools import TOOLS  # noqa: E402


async def main() -> int:
    expected_names = {spec.name for spec in TOOLS}

    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--project", str(AGENT_ROOT), "survey-agent-mcp"],
        env={**os.environ, "CS14_MOCK": "1"},
        cwd=str(AGENT_ROOT),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            print(f"initialized: server={init_result.serverInfo.name} v{init_result.serverInfo.version}")

            list_result = await session.list_tools()
            listed = {t.name: t for t in list_result.tools}

    missing = expected_names - listed.keys()
    extra = listed.keys() - expected_names
    no_schema = [name for name, t in listed.items() if not t.inputSchema or not t.inputSchema.get("properties", {}) and t.inputSchema.get("required")]

    print(f"expected {len(expected_names)} tools, listed {len(listed)} tools")
    for name in sorted(listed):
        print(f"  - {name}")

    ok = True
    if missing:
        print(f"FAIL: missing tools not listed by MCP server: {sorted(missing)}")
        ok = False
    if extra:
        print(f"FAIL: MCP server listed tools not in TOOLS: {sorted(extra)}")
        ok = False
    if len(listed) != len(TOOLS):
        print(f"FAIL: tool count mismatch: TOOLS has {len(TOOLS)}, server listed {len(listed)}")
        ok = False

    if ok:
        print(f"PASS: all {len(TOOLS)} tools listed correctly via MCP tools/list")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
