"""
Librarian — the Library's content layer (MIMIR_WARDEN_SCOPE.md §3).

Phase 2 vendors rag-bench's `PaperChunker` as a self-contained, deterministic
chunker: NO DB, NO LLM, NO network. The Librarian loop calls
`PaperChunker(...).plan(doc)` to turn a `ParsedDoc` into `ChunkPlanItem` rows
ready for the `chunks` table (migration 015).
"""

from labfoundry.research.librarian.chunker import PaperChunker, get_strategy
from labfoundry.research.librarian.schemas import (
    ChunkPlanItem,
    DocumentKind,
    ParsedDoc,
)

__all__ = [
    "PaperChunker",
    "get_strategy",
    "ChunkPlanItem",
    "DocumentKind",
    "ParsedDoc",
]
