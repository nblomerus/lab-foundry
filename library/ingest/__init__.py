"""Library ingest substrate — deterministic parse/chunk/discover (no DB/LLM/network).
Re-exports kept for the chunker public API."""

from library.ingest.chunker import PaperChunker, get_strategy
from library.ingest.schemas import ChunkPlanItem, DocumentKind, ParsedDoc

__all__ = ["PaperChunker", "get_strategy", "ChunkPlanItem", "DocumentKind", "ParsedDoc"]
