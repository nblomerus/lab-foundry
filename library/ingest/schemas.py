"""
Schemas for the Librarian's chunker — the INPUT (`ParsedDoc`) and the OUTPUT
(`ChunkPlanItem`) of `PaperChunker.plan`.

These are deliberately decoupled from `labfoundry/research/schemas.py`. That
module describes the *output* path of the researcher loop (findings written to
the `findings` table). This module describes the *ingest* path of the Library
(documents/chunks written by the Librarian, migration 015). Keeping the two
literals separate is a locked design rule — `DocumentKind` below is a NEW
literal and must not reuse / extend `FindingOut.source`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# -------------------------------------------------------------------------
# Document kind — mirrors migration 015's `document_kind` enum exactly.
# ('paper','media','dataset','web','code','note')
# This is the ingest-side taxonomy; do NOT conflate with FindingOut.source.
# -------------------------------------------------------------------------

DocumentKind = Literal["paper", "media", "dataset", "web", "code", "note"]


# -------------------------------------------------------------------------
# ParsedDoc — the chunker's INPUT.
#
# A parsed document handed to the Librarian before chunking. `sections` maps a
# normalized section name (e.g. "introduction", "method") to its raw text;
# `acronyms` maps an acronym to its full form (e.g. "MIPS" -> "Maximum Inner
# Product Search") for first-use expansion per chunk.
# -------------------------------------------------------------------------


class ParsedDoc(BaseModel):
    doc_id: str | int = Field(..., description="Stable id of the source document.")
    title: str = Field(..., description="Document title; used in the contextual chunk prefix.")
    authors: list[str] = Field(default_factory=list, description="Author display names, for citation formatting.")
    year: int | None = Field(default=None, description="Publication year, if known.")
    sections: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized section name -> section text. Empty falls back to full_text.",
    )
    acronyms: dict[str, str] = Field(
        default_factory=dict,
        description="Acronym -> full form, expanded on first occurrence per chunk.",
    )
    doi: str | None = None
    arxiv_id: str | None = None
    categories: list[str] = Field(default_factory=list)
    topic: str | None = None
    full_text: str | None = Field(
        default=None,
        description="Whole-document text, used as a single section when `sections` is empty.",
    )


# -------------------------------------------------------------------------
# ChunkPlanItem — the chunker's OUTPUT row.
#
# One row per retrieval-ready chunk, aligned to migration 015's `chunks` table.
# The Librarian loop turns each item into a `chunks` INSERT:
#   ordinal      -> chunks.ordinal       (0-based, across the whole doc)
#   text         -> chunks.text
#   content_hash -> chunks.content_hash  (sha256(text); idempotent re-chunk)
#   token_count  -> chunks.token_count   (cheap estimate today)
#   section      -> carried in provenance / not a 015 column itself
# `document_id`, `embedding`, `embed_model` are filled by the Librarian when it
# resolves the document row and runs the embed step.
# -------------------------------------------------------------------------


class ChunkPlanItem(BaseModel):
    ordinal: int = Field(..., ge=0, description="0-based position within the document.")
    text: str = Field(..., description="The chunk text, including the contextual prefix.")
    content_hash: str = Field(..., description="sha256 hex digest of `text`.")
    token_count: int | None = Field(
        default=None,
        description="Cheap token estimate (len(text)//4); a real tokenizer may replace it.",
    )
    section: str = Field(..., description="Normalized source section this chunk came from.")
