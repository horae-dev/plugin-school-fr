# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys

import anyio
from horae_sdk.testing import read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_school_fr import COMMAND, NAME

TOOL = f"{NAME}.status"


async def serve_and_call() -> tuple[set[str], str | None]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:])
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        # no path is approved for writing here, so the call is refused before any file access
        replayed = read_result(await client.call_tool(TOOL, {"state_path": "/nonexistent/s.json"}))
    return tools, replayed.error


def test_serves_over_stdio() -> None:
    tools, error = anyio.run(serve_and_call)
    assert tools == {TOOL, "school-fr.pronote", "school-fr.ecoledirecte"}
    assert error is not None
    assert "path_not_approved" in error
