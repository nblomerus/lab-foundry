"""
FastMCP server wrapper for boardroom-knowledge.

Exposes read-only query tools over MCP:
  - get_claim_evidence_chain
  - get_claim_critics
  - get_finding_influence

Write tools (merge_*) are internal infrastructure, called by handlers.
"""
from mcp.server.fastmcp import FastMCP

from boardroom.mcp_servers.boardroom_knowledge.tools import (
    get_claim_evidence_chain,
    get_claim_critics,
    get_finding_influence,
)

mcp = FastMCP("boardroom-knowledge")

mcp.tool()(get_claim_evidence_chain)
mcp.tool()(get_claim_critics)
mcp.tool()(get_finding_influence)

if __name__ == "__main__":
    mcp.run()
