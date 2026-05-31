"""
Unit tests for the Librarian's paper parser (`parse_paper`).

Pure, deterministic — NO DB, NO LLM, NO network. Feeds a realistic arxiv-like
raw text (markdown section headers, an acronym definition, an equation block)
plus citation metadata, and asserts that `parse_paper`:
  * returns a fully-populated `ParsedDoc` (sections, acronyms, all metadata),
  * preserves the equation and section structure the chunker depends on, and
  * round-trips into `PaperChunker().plan(doc)` producing >0 chunk items.
"""

from __future__ import annotations

from labfoundry.research.librarian import ChunkPlanItem, PaperChunker, ParsedDoc
from labfoundry.research.librarian.parser import parse_paper

# A realistic arxiv-like markdown paper: multiple section headers, an acronym
# definition in-line, an equation block, and a blocklisted References section.
RAW_PAPER = """\
# Attention Is All You Need

## Abstract

We propose a new architecture for sequence transduction. The dominant
operation, Maximum Inner Product Search (MIPS), runs over learned
representations, and we revisit that operation in the large-scale regime.

## 1 Introduction

Recurrent models have long dominated sequence modeling. We argue that attention
alone suffices, removing recurrence entirely. Throughout the paper we lean on
MIPS as the core retrieval primitive and analyze its cost at scale across many
candidate vectors and very large indices.

## 2 Method

Our scaled dot-product attention is defined by the following relation:

$$ \\text{Attention}(Q, K, V) = \\text{softmax}\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V $$

This formulation keeps the computation differentiable end to end while remaining
efficient enough for the billion-scale setting we ultimately target in practice.

## References

[1] Someone et al. A prior paper about things. 2015.
[2] Another Person. Yet another paper worth citing here. 2016.
"""


def _parse() -> ParsedDoc:
    return parse_paper(
        RAW_PAPER,
        arxiv_id="1706.03762",
        doi="10.48550/arXiv.1706.03762",
        title="Attention Is All You Need",
        authors="Ashish Vaswani, Noam Shazeer, Niki Parmar",
        year=2017,
        categories=["cs.CL", "cs.LG"],
        url="https://arxiv.org/abs/1706.03762",
    )


def test_returns_parsed_doc():
    doc = _parse()
    assert isinstance(doc, ParsedDoc)


def test_metadata_fields_populated():
    doc = _parse()
    assert doc.title == "Attention Is All You Need"
    assert doc.authors == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
    assert doc.year == 2017
    assert doc.arxiv_id == "1706.03762"
    assert doc.doi == "10.48550/arXiv.1706.03762"
    assert doc.categories == ["cs.CL", "cs.LG"]
    assert doc.topic == "cs.CL"  # derived from first category, no LLM
    assert doc.doc_id == "arxiv_1706.03762"
    assert doc.full_text and "scaled dot-product attention" in doc.full_text.lower()


def test_multiple_sections_extracted():
    doc = _parse()
    # The markdown headers must yield several normalized sections.
    assert len(doc.sections) >= 3
    assert "abstract" in doc.sections
    assert "introduction" in doc.sections
    assert "method" in doc.sections
    # Section text is preserved (not collapsed away).
    assert "Recurrent models" in doc.sections["introduction"]


def test_acronym_captured():
    doc = _parse()
    assert doc.acronyms.get("MIPS") == "Maximum Inner Product Search"


def test_equation_preserved_in_section_text():
    doc = _parse()
    # The full equation block must survive intact inside the method section so
    # the chunker can protect it from splitting.
    method = doc.sections["method"]
    assert "\\text{Attention}(Q, K, V)" in method
    assert "\\frac{QK^T}{\\sqrt{d_k}}" in method


def test_year_falls_back_to_arxiv_id_when_missing():
    doc = parse_paper(RAW_PAPER, arxiv_id="1706.03762", title="X")
    # 1706 -> YYMM -> 2017 (yy < 50 => 2000 + yy).
    assert doc.year == 2017


def test_round_trips_into_chunker():
    doc = _parse()
    items = PaperChunker().plan(doc)
    assert items, "expected >0 chunk items from the parsed doc"
    assert all(isinstance(it, ChunkPlanItem) for it in items)
    # The blocklisted References section must not produce chunks.
    assert all(it.section != "references" for it in items)
    # The acronym is expanded on first use in some chunk (chunker behavior,
    # enabled by the acronyms dict the parser populated).
    assert any("Maximum Inner Product Search (MIPS)" in it.text for it in items)
    # The equation survives the chunk round-trip unbroken.
    full = "\n\n".join(it.text for it in items)
    assert "\\frac{QK^T}{\\sqrt{d_k}}" in full
