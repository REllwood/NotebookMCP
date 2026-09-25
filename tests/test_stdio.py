"""Launch the real server process the way Claude Code and Codex do, over stdio."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from notebookmcp.server import NOT_SIGNED_IN


async def test_stdio_server_without_login(tmp_path: Path) -> None:
    env = {**os.environ, "NOTEBOOKLM_HOME": str(tmp_path)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "notebookmcp"], env=env)
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 6
        result = await client.call_tool("list_notebooks", {})
    assert result.is_error
    assert result.content[0].text.endswith(NOT_SIGNED_IN)
