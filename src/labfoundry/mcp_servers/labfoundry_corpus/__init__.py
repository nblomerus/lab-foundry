"""
labfoundry_corpus — the Library's RAG read path over the pgvector corpus (§6 of
MIMIR_WARDEN_SCOPE.md).

The real integration is **direct in-process import** (the Researcher/Novelty loops
import these plain async functions); ``server.py`` also exposes the same four tools
over MCP. Per §6, ``TOOLS_BY_AGENT`` ``tool_names`` is dead code — import these
functions directly rather than relying on MCP tool-calling.
"""

from labfoundry.mcp_servers.labfoundry_corpus.tools import (
    ContextBlock,
    DatasetRow,
    DocumentDetail,
    ProvenanceSpan,
    RetrievedChunk,
    build_context,
    corpus_get_document,
    corpus_search,
    list_datasets,
)

__all__ = [
    "corpus_search",
    "build_context",
    "corpus_get_document",
    "list_datasets",
    "RetrievedChunk",
    "ContextBlock",
    "ProvenanceSpan",
    "DocumentDetail",
    "DatasetRow",
]
