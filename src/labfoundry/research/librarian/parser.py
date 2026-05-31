"""
parser.py — Parse already-fetched paper text + metadata into a `ParsedDoc`.

Vendored from rag-bench (`rag_bench/core/ingest.py` and its helper
`rag_bench/utils/text.py`) for the Library's Librarian (MIMIR_WARDEN_SCOPE.md
§3, Phase 2). Reuses rag-bench's proven, deterministic section / acronym /
metadata extraction — the Director's directive was "reuse rag parse, it works
already", so NO new LLM parser is introduced here.

Self-contained and deterministic: NO DB, NO LLM, NO network. This is the PARSE
half only.

  WHAT WAS SPLIT OUT
  ------------------
  rag-bench's `ingest.py` COUPLED fetch + parse:
    * `load_arxiv_dataset()` / `ingest_dataset()` FETCH from HuggingFace
      (`datasets.load_dataset(...)`) — network + a heavy `datasets` dep.
    * `parse_paper(row: dict)` PARSES a dataset row into a document dict.
  Only the PARSE half is vendored here. Fetching is a different module's job;
  `parse_paper` below takes already-fetched `raw_text` + metadata kwargs (rather
  than a HuggingFace row dict) and returns a `ParsedDoc` the chunker consumes.

  WHAT IS REUSED vs ADAPTED (full manifest at module end)
  -------------------------------------------------------
  * REUSED verbatim (logic copied from `rag_bench/utils/text.py`):
      - SECTION_KEYWORDS, HEADER_PATTERN, PDF_HEADER_PATTERNS, _SECTION_NUMBER_RE
      - normalize_section_name(), extract_sections(), extract_sections_from_pdf()
      - build_acronym_dict()
      - fix_encoding() + _ENCODING_FIXES, clean_latex_artifacts()
    and from `rag_bench/core/ingest.py`:
      - extract_year() heuristics (year field -> date fields -> arxiv id YYMM).
  * ADAPTED:
      - `parse_paper` signature: from `(row: dict) -> dict` to
        `(raw_text, *, metadata kwargs) -> ParsedDoc`.
      - section extraction now tries markdown headers first and falls back to
        the PDF-style extractor when markdown finds nothing useful (rag-bench's
        ingest only ever called the markdown `extract_sections`).
      - NEW pass-through metadata the chunker reads but rag-bench's ingest dict
        never carried: `doi`, `categories`, `topic`, `url`.

The output `ParsedDoc` is the exact input `PaperChunker().plan(doc)` expects;
see `labfoundry/research/librarian/schemas.py`.
"""

from __future__ import annotations

import re

from labfoundry.research.librarian.schemas import ParsedDoc

# ═══════════════════════════════════════════════════════════════════════════
# Section header patterns — copied verbatim from rag_bench/utils/text.py.
# ═══════════════════════════════════════════════════════════════════════════

# Section header keywords common in AI/ML papers. Kept for documentation /
# reuse parity with rag-bench; the regex patterns below are what actually drive
# extraction.
SECTION_KEYWORDS = [
    "abstract",
    "introduction",
    "related work",
    "background",
    "preliminary",
    "preliminaries",
    "problem setup",
    "problem statement",
    "method",
    "methodology",
    "methods",
    "approach",
    "model",
    "architecture",
    "framework",
    "system",
    "attention",
    "multi-head attention",
    "self-attention",
    "training",
    "training objective",
    "training procedure",
    "optimization",
    "learning",
    "experiment",
    "experiments",
    "experimental setup",
    "experimental results",
    "evaluation",
    "results",
    "main results",
    "analysis",
    "ablation",
    "ablation study",
    "discussion",
    "conclusion",
    "conclusions",
    "limitation",
    "limitations",
    "broader impact",
    "appendix",
    "supplementary",
]

# Compiled pattern for detecting section headers in markdown.
HEADER_PATTERN = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

# Additional patterns for PDF-extracted text (plain text headers).
PDF_HEADER_PATTERNS = [
    re.compile(r"^(\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z\s]+)$"),  # Numbered: 1. Introduction
    re.compile(r"^([A-Z][A-Z\s]{3,40})$"),  # ALL CAPS: INTRODUCTION
    re.compile(
        r"^(Abstract|Introduction|Related Work|Background|"
        r"Method(?:ology|s)?|Approach|Model|Architecture|"
        r"Experiment(?:s|al)?(?:\s+(?:Setup|Results))?|Results|"
        r"Discussion|Conclusion(?:s)?|Limitation(?:s)?|"
        r"Training|Evaluation|Analysis|Appendix)\s*$",
        re.IGNORECASE,
    ),
]

# Standalone section number on its own line (PDF extraction splits number off).
_SECTION_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*$")


# ═══════════════════════════════════════════════════════════════════════════
# Section extraction — copied verbatim from rag_bench/utils/text.py.
# ═══════════════════════════════════════════════════════════════════════════


def normalize_section_name(name: str) -> str:
    """Normalize a section header into a consistent key."""
    # Remove numbering like "3.1", "IV.", etc.
    name = re.sub(r"^[\d.]+\s*", "", name)
    name = re.sub(r"^[IVXLC]+\.\s*", "", name)

    # Lowercase and strip
    name = name.lower().strip()

    # Remove trailing punctuation
    name = re.sub(r"[:\-–—]+$", "", name).strip()

    # Replace spaces/special chars with underscores
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")

    return name or "unnamed"


def extract_sections(text: str) -> dict[str, str]:
    """
    Split paper text into sections based on markdown headers.

    Returns a dict mapping normalized section names to their text content.
    Handles nested headers (##, ###) by flattening to top-level sections.
    """
    if not text or not text.strip():
        return {"full_text": ""}

    sections: dict[str, str] = {}
    current_section = "preamble"
    current_lines: list[str] = []

    for line in text.split("\n"):
        header_match = HEADER_PATTERN.match(line.strip())
        if header_match:
            # Save previous section
            if current_lines:
                section_text = "\n".join(current_lines).strip()
                if section_text:
                    sections[current_section] = section_text

            # Normalize section name
            raw_name = header_match.group(2).strip()
            current_section = normalize_section_name(raw_name)
            current_lines = []
        else:
            current_lines.append(line)

    # Save final section
    if current_lines:
        section_text = "\n".join(current_lines).strip()
        if section_text:
            sections[current_section] = section_text

    return sections


def extract_sections_from_pdf(text: str) -> dict[str, str]:
    """
    Split PDF-extracted text into sections.

    Handles both markdown-style and plain text headers found in PDF extractions.
    Also handles split headers where a section number (e.g. '3.2.1') appears on
    one line and the title (e.g. 'Scaled Dot-Product Attention') on the next.
    """
    if not text or len(text.strip()) < 100:
        return {"full_text": text}

    sections: dict[str, str] = {}
    current_section = "preamble"
    current_lines: list[str] = []

    # Combined patterns: markdown + PDF-specific
    all_patterns = [HEADER_PATTERN] + PDF_HEADER_PATTERNS
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        matched_header = None

        for pattern in all_patterns:
            m = pattern.match(stripped)
            if m:
                if pattern is HEADER_PATTERN:
                    matched_header = m.group(2).strip()
                else:
                    matched_header = m.group(1) if m.lastindex else stripped
                break

        # Check for split header: standalone section number + title on next line
        if not matched_header and _SECTION_NUMBER_RE.match(stripped) and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            # Next line should look like a title: starts uppercase, short, no period
            if next_line and next_line[0].isupper() and len(next_line) < 60 and not next_line.endswith("."):
                matched_header = f"{stripped} {next_line}"
                i += 1  # consume the title line too

        if matched_header and len(matched_header) < 80:
            # Save previous section
            if current_lines:
                section_text = "\n".join(current_lines).strip()
                if section_text and len(section_text) > 30:
                    sections[current_section] = section_text

            current_section = normalize_section_name(matched_header)
            current_lines = []
        else:
            current_lines.append(lines[i])

        i += 1

    # Save final section
    if current_lines:
        section_text = "\n".join(current_lines).strip()
        if section_text and len(section_text) > 30:
            sections[current_section] = section_text

    return sections if sections else {"full_text": text}


# ═══════════════════════════════════════════════════════════════════════════
# Acronym extraction — copied verbatim from rag_bench/utils/text.py.
# ═══════════════════════════════════════════════════════════════════════════


def build_acronym_dict(text: str) -> dict[str, str]:
    """
    Extract acronym definitions from paper text.

    Looks for patterns like:
    - "Maximum Inner Product Search (MIPS)"
    - "reinforcement learning from human feedback (RLHF)"
    """
    acronyms: dict[str, str] = {}

    # Pattern: "Full Name (ACRONYM)"
    pattern = r"([A-Za-z][A-Za-z\s\-]{2,50})\s*\(([A-Z][A-Z0-9]{1,10})\)"
    for match in re.finditer(pattern, text):
        full_form = match.group(1).strip()
        acronym = match.group(2).strip()

        # Validate: full form should be at least two words
        words = [w for w in full_form.split() if w[0].isupper() or w[0].islower()]
        if len(words) >= 2:
            acronyms[acronym] = full_form

    return acronyms


# ═══════════════════════════════════════════════════════════════════════════
# Encoding / LaTeX cleanup — copied verbatim from rag_bench/utils/text.py.
# ═══════════════════════════════════════════════════════════════════════════
# Map of garbled UTF-8-as-Latin-1 sequences to correct Unicode characters.
_ENCODING_FIXES = {
    # Greek letters (very common in ML papers)
    "Î±": "α",
    "Î²": "β",
    "Î³": "γ",
    "Î´": "δ",
    "Îµ": "ε",
    "Î¶": "ζ",
    "Î·": "η",
    "Î¸": "θ",
    "Î¹": "ι",
    "Îº": "κ",
    "Î»": "λ",
    "Î¼": "μ",
    "Î½": "ν",
    "Î¾": "ξ",
    "Î¿": "ο",
    "Ï€": "π",
    "Ï": "ρ",
    "Ïƒ": "σ",
    "Ï„": "τ",
    "Ï…": "υ",
    "Ï†": "φ",
    "Ï‡": "χ",
    "Ïˆ": "ψ",
    "Ï‰": "ω",
    "Ïµ": "ε",
    "Ï²": "ρ",
    # Math symbols
    "â‰¤": "≤",
    "â‰¥": "≥",
    "â‰ˆ": "≈",
    "â†'": "→",
    "Ã—": "×",
    "Ã·": "÷",
    "Â±": "±",
    "âˆž": "∞",
    "âˆ'": "∑",
    "âˆš": "√",
    "âˆ‚": "∂",
    "âˆ†": "∆",
    "âˆ‡": "∇",
    "âˆˆ": "∈",
    "âˆ©": "∩",
    "âˆª": "∪",
    "âˆ¼": "∼",
    "Â·": "·",
    # Common accented chars & special
    "Âµ": "μ",
    "Ã¡": "á",
    "Ã©": "é",
    "Ã³": "ó",
    "Ã¶": "ö",
    "Ã¼": "ü",
    "Ã±": "ñ",
    "Ã§": "ç",
    # Subscript/superscript
    "Â²": "²",
    "Â³": "³",
}


def fix_encoding(text: str) -> str:
    """Fix common garbled UTF-8-as-Latin-1 encoding artifacts.

    Also attempts the more general fix: try re-encoding as latin-1 and decoding
    as utf-8 on segments that look garbled.
    """
    if not text:
        return text

    # General fix: if the text has telltale garbled patterns, try to re-encode.
    try:
        fixed = text.encode("latin-1").decode("utf-8")
        if fixed.count("Â") + fixed.count("Ã") < text.count("Â") + text.count("Ã"):
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # Fall back to lookup-table replacement for partial garbling.
    for garbled, correct in _ENCODING_FIXES.items():
        if garbled in text:
            text = text.replace(garbled, correct)

    return text


def clean_latex_artifacts(text: str) -> str:
    """
    Clean up broken LaTeX formatting from dataset/PDF conversion.

    NOTE (intentional deviation from rag-bench): rag-bench's `clean_latex_artifacts`
    collapsed ALL whitespace (``\\s+`` -> single space), which would flatten the
    newline-delimited section/table structure the chunker relies on. We therefore
    only apply the *safe* subset here (zero-width chars, doubled math symbols,
    matrix notation) and DO NOT collapse newlines — preserving section structure,
    equations, and table rows for `PaperChunker`. The aggressive whitespace
    collapse lives only inside per-chunk cleanup in the chunker.
    """
    if not text:
        return text

    # Remove zero-width spaces and similar Unicode artifacts.
    text = text.replace("​", "")  # zero-width space
    text = text.replace("﻿", "")  # zero-width no-break space
    text = text.replace(" ", " ")  # non-breaking space → regular space

    # Fix doubled mathematical symbols.
    text = re.sub(r"∈\s*∈", "∈", text)
    text = re.sub(r"×\s*×", "×", text)
    text = re.sub(r"∀\s*∀", "∀", text)
    text = re.sub(r"∃\s*∃", "∃", text)
    text = re.sub(r"∇\s*∇", "∇", text)

    # Fix broken matrix notation (e.g., "Rn × ×d" → "R^{n×d}").
    text = re.sub(r"R([a-z])\s*×\s*×\s*([a-z])", r"R^{\1×\2}", text)
    text = re.sub(r"R([a-z])\s*×\s*([a-z])", r"R^{\1×\2}", text)

    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════
# Year extraction — adapted from rag_bench/core/ingest.py:extract_year.
# Originally took a HuggingFace `row` dict; here it takes the explicit values
# the caller already has plus the arxiv id, with the same heuristic order.
# ═══════════════════════════════════════════════════════════════════════════


def _extract_year(year: int | str | None, arxiv_id: str | None) -> int | None:
    """Resolve publication year: explicit value first, then the YYMM arxiv id.

    Mirrors rag-bench's heuristic order (direct year field -> date fields ->
    arxiv id YYMM.NNNNN). Date-field parsing is folded into the explicit
    `year` argument here since the caller passes already-fetched metadata.
    """
    # Direct year value (may be an int, or a string/date-like blob).
    if year:
        try:
            return int(year)
        except (ValueError, TypeError):
            match = re.search(r"(20\d{2}|19\d{2})", str(year))
            if match:
                return int(match.group(1))

    # Fall back to the arxiv id (format: YYMM.NNNNN).
    if arxiv_id:
        match = re.match(r"(\d{2})(\d{2})\.", str(arxiv_id))
        if match:
            yy = int(match.group(1))
            return 2000 + yy if yy < 50 else 1900 + yy

    return None


# ═══════════════════════════════════════════════════════════════════════════
# parse_paper — ADAPTED from rag_bench/core/ingest.py:parse_paper.
#
# rag-bench's version took a HuggingFace dataset `row: dict` and returned a
# plain document dict. This version takes already-fetched `raw_text` plus
# explicit metadata kwargs and returns a `ParsedDoc` (the chunker's input). The
# section/acronym/year extraction logic is reused unchanged.
# ═══════════════════════════════════════════════════════════════════════════


def parse_paper(
    raw_text: str,
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
    authors: list[str] | str | None = None,
    year: int | str | None = None,
    categories: list[str] | str | None = None,
    url: str | None = None,
) -> ParsedDoc:
    """Parse already-fetched paper text + metadata into a `ParsedDoc`.

    Deterministic and heuristic — NO LLM, NO network. Reuses rag-bench's
    section / acronym / metadata extraction so the resulting `ParsedDoc` is
    FULLY populated (sections, acronyms, equations and tables preserved inside
    section text, plus all citation metadata) — exactly what `PaperChunker`
    depends on.

    Args:
        raw_text:   The full, already-fetched paper text (markdown or PDF-extracted).
        arxiv_id:   arXiv identifier, e.g. "1706.03762". Used for the doc_id and
                    as a year fallback (YYMM).
        doi:        DOI string, if known.
        title:      Document title. Falls back to "Unknown".
        authors:    Author list, or a comma-separated string (normalized to a list).
        year:       Publication year (int) or a date-like string to mine a year from.
        categories: Subject categories, e.g. ["cs.CL"] or "cs.CL,cs.LG".
        url:        Canonical URL. Becomes the doc_id when no arxiv_id/doi is given.

    Returns:
        A `ParsedDoc` ready for `PaperChunker().plan(doc)`.
    """
    # ── Clean the raw text (encoding + safe LaTeX artifacts) ──
    text = raw_text or ""
    text = fix_encoding(text)
    text = clean_latex_artifacts(text)

    # ── Title ──
    if isinstance(title, list):
        title = title[0] if title else "Unknown"
    title = title.strip() if isinstance(title, str) and title.strip() else "Unknown"

    # ── Authors (reuse rag-bench: comma-split a string into a list) ──
    if authors is None:
        author_list: list[str] = []
    elif isinstance(authors, str):
        author_list = [a.strip() for a in authors.split(",") if a.strip()]
    else:
        author_list = [str(a).strip() for a in authors if str(a).strip()]

    # ── Categories (accept list or comma-separated string) ──
    if categories is None:
        category_list: list[str] = []
    elif isinstance(categories, str):
        category_list = [c.strip() for c in categories.split(",") if c.strip()]
    else:
        category_list = [str(c).strip() for c in categories if str(c).strip()]

    # ── Identifiers / doc_id (reuse rag-bench's arxiv_<id> shape) ──
    arxiv_id = str(arxiv_id) if arxiv_id else None
    doi = str(doi) if doi else None
    if arxiv_id:
        doc_id = f"arxiv_{arxiv_id}".replace("/", "_")
    elif doi:
        doc_id = f"doi_{doi}".replace("/", "_")
    elif url:
        doc_id = url
    else:
        doc_id = "unknown"

    # ── Sections: markdown first, fall back to the PDF-style extractor ──
    # rag-bench's ingest only called the markdown extractor; we try it, and if
    # it produced no real structure (only the synthetic preamble/full_text),
    # fall back to the PDF header heuristics so PDF-extracted text also works.
    sections = extract_sections(text)
    if not _has_real_sections(sections):
        pdf_sections = extract_sections_from_pdf(text)
        if _has_real_sections(pdf_sections):
            sections = pdf_sections

    # ── Acronyms ──
    acronyms = build_acronym_dict(text)

    # ── topic: lightweight derivation from the first category (no LLM) ──
    topic = category_list[0] if category_list else None

    return ParsedDoc(
        doc_id=doc_id,
        title=title,
        authors=author_list,
        year=_extract_year(year, arxiv_id),
        sections=sections,
        acronyms=acronyms,
        doi=doi,
        arxiv_id=arxiv_id,
        categories=category_list,
        topic=topic,
        full_text=text,
    )


def _has_real_sections(sections: dict[str, str]) -> bool:
    """True if `sections` carries genuine structure (not just a single
    synthetic preamble/full_text bucket)."""
    if not sections:
        return False
    keys = set(sections)
    synthetic = {"preamble", "full_text"}
    # Real structure = at least one named section beyond the synthetic buckets.
    return bool(keys - synthetic)
