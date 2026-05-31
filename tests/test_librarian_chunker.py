"""
Unit tests for the Librarian's vendored chunker.

Pure, deterministic — NO DB, NO LLM, NO network. Builds a synthetic ParsedDoc
and asserts the domain-aware behaviors rag-bench's PaperChunker is responsible
for: blocklist filtering, equation atomicity, acronym expansion, the contextual
prefix, and the plan() row shape (sha256 hashes + monotonic 0-based ordinals).
Assertions stay robust to exact chunk boundaries.
"""

from __future__ import annotations

import hashlib

from labfoundry.research.librarian import ChunkPlanItem, PaperChunker, ParsedDoc


def _make_doc() -> ParsedDoc:
    # A normal section, long enough to clear min_section_length and to be a
    # substantive chunk after the MIN_CHUNK_LENGTH (100 char) floor.
    intro = (
        "We study fast retrieval over very large corpora. The core operation "
        "is MIPS, which we apply repeatedly across many candidate vectors. "
        "Prior work shows that approximate methods trade recall for speed, and "
        "we revisit that tradeoff in the modern regime of billion-scale indices."
    )

    # A section carrying an equation block ($$...$$) and a small markdown table.
    # The equation must survive intact in some chunk (never split mid-equation).
    method = (
        "Our objective derives from the classical mass-energy relation.\n\n"
        "$$ E = mc^2 + \\sum_{i=1}^{n} \\alpha_i x_i $$\n\n"
        "We report the headline numbers below, where MIPS again drives cost.\n\n"
        "| System | Recall | QPS |\n"
        "| --- | --- | --- |\n"
        "| ours | 0.97 | 1200 |\n"
        "| baseline | 0.91 | 800 |\n\n"
        "The table shows our system dominates the baseline on both axes while "
        "keeping the same memory budget, which is the central claim of the work."
    )

    # A blocklisted section that must be dropped entirely.
    references = (
        "[1] Someone et al. A paper about things. 2020.\n"
        "[2] Another Person. Yet another paper worth citing here. 2021.\n"
        "[3] A Third Author. The final reference in this dropped section. 2022."
    )

    return ParsedDoc(
        doc_id="doc-1",
        title="Fast Retrieval",
        authors=["Ada Lovelace", "Alan Turing"],
        year=2024,
        sections={
            "introduction": intro,
            "method": method,
            "references": references,
        },
        acronyms={"MIPS": "Maximum Inner Product Search"},
        topic="retrieval",
        categories=["cs.IR"],
    )


def test_plan_returns_chunk_plan_items():
    items = PaperChunker().plan(_make_doc())
    assert items, "expected at least one chunk"
    assert all(isinstance(it, ChunkPlanItem) for it in items)


def test_references_section_is_dropped():
    items = PaperChunker().plan(_make_doc())
    # No chunk should come from the blocklisted 'references' section.
    assert all(it.section != "references" for it in items)
    # Sanity: the kept sections are the non-blocklisted ones.
    assert set(it.section for it in items) <= {"introduction", "method"}


def test_equation_block_survives_intact():
    items = PaperChunker().plan(_make_doc())
    full = "\n\n".join(it.text for it in items)
    # The equation must appear somewhere, unbroken (not split mid-equation).
    assert "$$ E = mc^2 + \\sum_{i=1}^{n} \\alpha_i x_i $$" in full
    # And it must live wholly inside a single chunk.
    assert any("$$ E = mc^2 + \\sum_{i=1}^{n} \\alpha_i x_i $$" in it.text for it in items)


def test_ordinals_monotonic_from_zero_and_hashes_match():
    items = PaperChunker().plan(_make_doc())
    ordinals = [it.ordinal for it in items]
    assert ordinals == list(range(len(items))), "ordinals must be 0-based and contiguous"
    # content_hash is sha256 of the (prefixed) text.
    for it in items:
        assert it.content_hash == hashlib.sha256(it.text.encode()).hexdigest()
    # Hashes are unique-ish: with distinct chunk texts they should not collide.
    assert len(set(it.content_hash for it in items)) == len(items)
    # token_count is the cheap char/4 estimate.
    for it in items:
        assert it.token_count == len(it.text) // 4


def test_contextual_prefix_present():
    items = PaperChunker().plan(_make_doc())
    # Every chunk is prefixed with "{title} — {Section}".
    assert all(it.text.startswith("Fast Retrieval — ") for it in items)
    # Section label is title-cased from the normalized section name.
    assert any(it.text.startswith("Fast Retrieval — Introduction") for it in items)
    assert any(it.text.startswith("Fast Retrieval — Method") for it in items)


def test_acronym_expanded_on_first_use():
    items = PaperChunker().plan(_make_doc())
    # At least one chunk should expand the first MIPS occurrence to its full form.
    assert any("Maximum Inner Product Search (MIPS)" in it.text for it in items)


def test_empty_sections_fall_back_to_full_text():
    doc = ParsedDoc(
        doc_id=7,
        title="Solo",
        authors=["Grace Hopper"],
        year=2019,
        sections={},
        full_text=(
            "This document has no sections, only a long stretch of full text "
            "that should still be chunked under the synthetic full_text section "
            "so that downstream retrieval has something to index against later."
        ),
    )
    items = PaperChunker().plan(doc)
    assert items
    assert all(it.section == "full_text" for it in items)
    assert all(it.text.startswith("Solo — Full Text") for it in items)
