"""
FastMCP server wrapper for labfoundry-corpus.

Exposes read-only corpus retrieval tools over MCP (Plane 1, ungated):
  - corpus_search
  - build_context
  - corpus_get_document
  - list_datasets

Internal infrastructure (_get_pool, Embedder, _search_by_vector) stays in tools.py
and is NOT exposed. The same functions are imported directly in-process by the
Researcher/Novelty loops — that direct import, not MCP tool-calling, is the real
integration path (§6).
"""

from mcp.server.fastmcp import FastMCP

from labfoundry.mcp_servers.labfoundry_corpus.tools import (
    build_context,
    corpus_get_document,
    corpus_search,
    list_datasets,
)

mcp = FastMCP("labfoundry-corpus")

mcp.tool()(corpus_search)
mcp.tool()(build_context)
mcp.tool()(corpus_get_document)
mcp.tool()(list_datasets)

if __name__ == "__main__":
    mcp.run()
