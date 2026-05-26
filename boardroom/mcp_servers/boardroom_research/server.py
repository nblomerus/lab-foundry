"""
boardroom-research MCP server.

Thin wrapper exposing the research tools (tools.py) over the Model Context
Protocol. In-process callers (the Researcher handler) import tools.py
directly; external agents go through this server.

Run as:
    python -m boardroom.mcp_servers.boardroom_research.server
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from boardroom.mcp_servers.boardroom_research.tools import (
    fetch_url,
    search_hacker_news,
    search_reddit,
    search_web,
)


mcp = FastMCP("boardroom-research")

# Register the plain async functions as MCP tools.
mcp.tool()(search_hacker_news)
mcp.tool()(search_web)
mcp.tool()(search_reddit)
mcp.tool()(fetch_url)


if __name__ == "__main__":
    mcp.run()  # stdio transport
