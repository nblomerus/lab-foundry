"""Unit tests for two unrelated-but-grouped targets, both fully mocked (NO real
Postgres/Neo4j/Ollama/network, NO DATABASE_URL fixture):

  * ops.ariadne_reflect  — the read-only REFLECT & STEER dry-run CLI. asyncpg.create_pool is
    monkeypatched to a ScriptedPool, load_dotenv is a no-op, and run_reflection / grade_reflection
    / PostgresClient are patched on the module. Drives run() through its happy path (verdicts +
    lessons rendered + grades), the no-standing-directions branch, the no-DSN guard, the
    DEEPSEEK_API_KEY warn branch, and the failing-grade / invalid-refs / no-lessons branches.

  * library.ingest.parser — the deterministic paper parser. The existing test_librarian_parser.py
    exercises the markdown happy path; this fills the gaps it misses: empty/whitespace text, the
    PDF-style section extractor (incl. split numbered headers), encoding fixes (latin-1 re-encode +
    lookup table), latex cleanup short-circuits, year mining from strings, and the parse_paper
    metadata edge cases (title/authors/categories as lists/strings, doc_id from doi/url/unknown,
    PDF fallback when markdown finds no structure).
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock

import pytest

from library.ingest.parser import (
    _extract_year,
    _has_real_sections,
    build_acronym_dict,
    clean_latex_artifacts,
    extract_sections,
    extract_sections_from_pdf,
    fix_encoding,
    parse_paper,
)
from ops import ariadne_reflect
from tests._helpers import ScriptedPool

# ═══════════════════════════════════════════════════════════════════════════
# ops.ariadne_reflect
# ═══════════════════════════════════════════════════════════════════════════


def _verdict(claim_id=7, assessment="advance", new_priority=None, reason="solid evidence"):
    return types.SimpleNamespace(claim_id=claim_id, assessment=assessment, new_priority=new_priority, reason=reason)


def _lesson(lesson="prefer cheap pilots", applies_when="when budget is tight"):
    return types.SimpleNamespace(lesson=lesson, applies_when=applies_when)


def _reflection(verdicts=None, lessons=None):
    return types.SimpleNamespace(
        portfolio_assessment="balanced but over-indexed on retrieval",
        verdicts=verdicts if verdicts is not None else [_verdict()],
        lessons=lessons if lessons is not None else [_lesson()],
        reprioritized_focus="double down on agentic eval",
    )


def _grade(passed=True, invalid=None, verdicts_valid=1.0, n_verdicts=1, n_lessons=1):
    return types.SimpleNamespace(
        verdicts_valid=verdicts_valid,
        n_verdicts=n_verdicts,
        invalid_refs=invalid or [],
        n_lessons=n_lessons,
        passed=passed,
    )


def _no_dotenv(monkeypatch):
    monkeypatch.setattr(ariadne_reflect, "load_dotenv", lambda *a, **k: None, raising=False)


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr(ariadne_reflect.asyncpg, "create_pool", AsyncMock(return_value=pool))


@pytest.mark.asyncio
async def test_reflect_happy(monkeypatch, capsys):
    """Full render: a re-prioritized verdict + lessons + a passing grade."""
    _patch_pool(monkeypatch, ScriptedPool([]))
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_reflect, "PostgresClient", lambda pool: "STATE")
    out = _reflection(verdicts=[_verdict(assessment="reprioritize", new_priority=8)])
    monkeypatch.setattr(ariadne_reflect, "run_reflection", AsyncMock(return_value=(out, [7])))
    monkeypatch.setattr(ariadne_reflect, "grade_reflection", lambda o, ids: _grade())

    rc = await ariadne_reflect.run()
    captured = capsys.readouterr()
    assert rc == 0
    assert "reflect & steer" in captured.out
    assert "balanced but over-indexed on retrieval" in captured.out
    # the verdict mark + priority annotation render
    assert "↕ REPRIORITIZE" in captured.out
    assert "direction #7" in captured.out and "priority=8" in captured.out
    assert "solid evidence" in captured.out
    # lessons block + the applies_when annotation
    assert "STRATEGIC LESSONS" in captured.out
    assert "prefer cheap pilots" in captured.out and "when budget is tight" in captured.out
    assert "double down on agentic eval" in captured.out
    assert "SHADOW MODE" in captured.out
    # grade block — passing
    assert "verdicts reference real standing ids" in captured.out
    assert "PASS — eligible to persist" in captured.out


@pytest.mark.asyncio
async def test_reflect_advance_no_priority_no_lessons_unknown_mark(monkeypatch, capsys):
    """An 'advance' verdict (no priority annotation), an unknown assessment (falls through to the
    raw string), and NO lessons (the lessons block is skipped)."""
    _patch_pool(monkeypatch, ScriptedPool([]))
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_reflect, "PostgresClient", lambda pool: "STATE")
    verdicts = [_verdict(assessment="advance"), _verdict(claim_id=9, assessment="mystery")]
    out = _reflection(verdicts=verdicts, lessons=[])
    monkeypatch.setattr(ariadne_reflect, "run_reflection", AsyncMock(return_value=(out, [7, 9])))
    monkeypatch.setattr(ariadne_reflect, "grade_reflection", lambda o, ids: _grade(n_verdicts=2))

    rc = await ariadne_reflect.run()
    captured = capsys.readouterr()
    assert rc == 0
    assert "→ ADVANCE" in captured.out
    assert "priority=" not in captured.out  # new_priority is None → no annotation
    assert "mystery" in captured.out  # unknown assessment renders verbatim
    assert "STRATEGIC LESSONS" not in captured.out  # empty lessons → block skipped


@pytest.mark.asyncio
async def test_reflect_failing_grade_with_invalid_refs(monkeypatch, capsys):
    """A verdict that references a hallucinated id → invalid_refs printed + NOT-yet footer, and a
    lesson with no applies_when (the trailing annotation is omitted)."""
    _patch_pool(monkeypatch, ScriptedPool([]))
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_reflect, "PostgresClient", lambda pool: "STATE")
    out = _reflection(lessons=[_lesson(lesson="no-context lesson", applies_when=None)])
    monkeypatch.setattr(ariadne_reflect, "run_reflection", AsyncMock(return_value=(out, [7])))
    monkeypatch.setattr(
        ariadne_reflect,
        "grade_reflection",
        lambda o, ids: _grade(passed=False, invalid=[99], verdicts_valid=0.0),
    )

    rc = await ariadne_reflect.run()
    captured = capsys.readouterr()
    assert rc == 0
    assert "invalid refs (hallucinated ids): [99]" in captured.out
    assert "no-context lesson" in captured.out
    assert "(when:" not in captured.out  # applies_when None → no annotation
    assert "NOT yet" in captured.out


@pytest.mark.asyncio
async def test_reflect_no_standing_directions(monkeypatch, capsys):
    """run_reflection returns (None, ...) → the 'frame an agenda first' branch, rc 0."""
    _patch_pool(monkeypatch, ScriptedPool([]))
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ariadne_reflect, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(ariadne_reflect, "run_reflection", AsyncMock(return_value=(None, [])))

    rc = await ariadne_reflect.run()
    captured = capsys.readouterr()
    assert rc == 0
    assert "No standing directions to steer" in captured.out


@pytest.mark.asyncio
async def test_reflect_no_deepseek_warns(monkeypatch, capsys):
    """Missing DEEPSEEK_API_KEY warns (fallback to local Ollama) but still runs."""
    _patch_pool(monkeypatch, ScriptedPool([]))
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(ariadne_reflect, "PostgresClient", lambda pool: "STATE")
    monkeypatch.setattr(ariadne_reflect, "run_reflection", AsyncMock(return_value=(_reflection(), [7])))
    monkeypatch.setattr(ariadne_reflect, "grade_reflection", lambda o, ids: _grade())

    rc = await ariadne_reflect.run()
    assert rc == 0
    assert "fall back to the local Ollama model" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_reflect_no_dsn(monkeypatch, capsys):
    """No DATABASE_URL → guard returns 2 before any pool is created."""
    _no_dotenv(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = await ariadne_reflect.run()
    assert rc == 2
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_reflect_main(monkeypatch):
    """main() wraps run() in asyncio.run."""
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), 0)[1])
    monkeypatch.setattr(sys, "argv", ["ops.ariadne_reflect"])
    assert ariadne_reflect.main() == 0


# ═══════════════════════════════════════════════════════════════════════════
# library.ingest.parser — gap-filling for the lines the existing suite misses.
# ═══════════════════════════════════════════════════════════════════════════


# ── extract_sections: empty / whitespace short-circuit (line 156) ──
def test_extract_sections_empty_returns_full_text():
    assert extract_sections("") == {"full_text": ""}
    assert extract_sections("   \n  \t ") == {"full_text": ""}


def test_extract_sections_markdown_saves_previous_section():
    """Two markdown headers: the first section's accumulated body is flushed when the
    second header arrives (lines 167-169)."""
    text = "# Abstract\nThe abstract body.\n# Introduction\nThe intro body.\n"
    sections = extract_sections(text)
    assert sections["abstract"] == "The abstract body."
    assert sections["introduction"] == "The intro body."


# ── extract_sections_from_pdf (lines 195-248) ──
def test_extract_sections_from_pdf_too_short_returns_full_text():
    """< 100 chars of stripped text → the synthetic full_text bucket (line 195)."""
    text = "tiny body"
    assert extract_sections_from_pdf(text) == {"full_text": text}


def test_extract_sections_from_pdf_plain_and_caps_headers():
    """Numbered + ALL-CAPS + canonical headers all drive section splits, and a final
    section is flushed at the end. Bodies must exceed 30 chars to be kept."""
    text = (
        "Some preamble text that is reasonably long so the buffer is not dropped here.\n"
        "1. Introduction\n"
        "This introduction paragraph is clearly more than thirty characters long indeed.\n"
        "METHOD\n"
        "Here we describe our method in enough words to comfortably clear the threshold.\n"
        "Conclusion\n"
        "We conclude with a paragraph that again easily exceeds the thirty character floor.\n"
    )
    sections = extract_sections_from_pdf(text)
    assert "introduction" in sections
    assert "method" in sections
    assert "conclusion" in sections
    assert "preamble" in sections  # the leading buffer was flushed at the first header


def test_extract_sections_from_pdf_markdown_header_branch():
    """A markdown `#` header inside otherwise-PDF text exercises the HEADER_PATTERN
    group-2 branch (line 215)."""
    text = (
        "Preamble paragraph that is comfortably over the thirty character retention floor.\n"
        "# Method\n"
        "The method body here is also clearly more than thirty characters long for keeping.\n"
    )
    sections = extract_sections_from_pdf(text)
    assert "method" in sections


def test_extract_sections_from_pdf_number_without_title_next():
    """A standalone section number whose next line does NOT look like a title (lowercase /
    ends with a period) → no split header is formed (branch 224->228 false-side)."""
    text = (
        "Opening paragraph long enough to comfortably clear the thirty character minimum.\n"
        "4\n"
        "lowercase continuation that ends with a period.\n"
        "Trailing paragraph also long enough to be retained past the thirty char filter.\n"
    )
    sections = extract_sections_from_pdf(text)
    # the '4' is not promoted to a header → everything stays under the synthetic preamble
    assert "preamble" in sections


def test_extract_sections_from_pdf_split_numbered_header():
    """A standalone section number followed by a short Title on the next line is joined
    into one header (lines 221-227) and consumes the title line."""
    text = (
        "Lead-in paragraph long enough to survive the thirty character minimum filter.\n"
        "3.2.1\n"
        "Scaled Dot-Product Attention\n"
        "The body of this subsection is verbose enough to clear the thirty char minimum.\n"
    )
    sections = extract_sections_from_pdf(text)
    # the normalized name strips the leading number → "scaled_dot_product_attention"
    assert any(k == "scaled_dot_product_attention" for k in sections)


def test_extract_sections_from_pdf_only_short_bodies_returns_full_text():
    """Long enough overall (>100 chars) but every section's accumulated body is too short
    (<=30 chars) to be kept — back-to-back ALL-CAPS headers with tiny bodies — so `sections`
    ends empty and the function falls through to the {'full_text': text} fallback (line 248)."""
    text = (
        "INTRODUCTION\nshort body\n"
        "METHODS\ntiny body\n"
        "RESULTS\nbrief body\n"
        "DISCUSSION\nsmall body\n"
        "CONCLUSION\nend body\n"
        "BACKGROUND\nmore body\n"
        "EVALUATION\nlast body\n"
    )
    assert len(text.strip()) > 100
    out = extract_sections_from_pdf(text)
    assert out == {"full_text": text}


# ── fix_encoding (lines 353, 359-361, 366) ──
def test_fix_encoding_empty_returns_input():
    assert fix_encoding("") == ""
    assert fix_encoding(None) is None


def test_fix_encoding_latin1_reencode_path():
    """Text that is utf-8 bytes mis-decoded as latin-1 → the re-encode/decode repair
    fires and reduces the garble markers (lines 357-359)."""
    garbled = "café".encode().decode("latin-1")  # 'cafÃ©'
    assert "Ã" in garbled
    assert fix_encoding(garbled) == "café"


def test_fix_encoding_unicode_error_swallowed():
    """Text containing a char outside latin-1 (a real α) makes `.encode('latin-1')` raise
    UnicodeEncodeError, which is swallowed (lines 360-361); the lookup table then runs."""
    out = fix_encoding("rate α and a garble Î²")
    assert "α" in out  # untouched
    assert "β" in out  # lookup-table replacement still applied


def test_fix_encoding_lookup_table_fallback():
    """A standalone garbled token that does NOT round-trip via latin-1 re-encode falls
    through to the per-token lookup-table replacement (line 366)."""
    # 'Î±' is a known greek-alpha garble; surrounded by ascii it won't reduce Â/Ã counts
    # on the global re-encode, so the lookup table is what fixes it.
    out = fix_encoding("the coefficient Î± controls the rate")
    assert "α" in out


# ── clean_latex_artifacts (line 384) ──
def test_clean_latex_empty_returns_input():
    assert clean_latex_artifacts("") == ""
    assert clean_latex_artifacts(None) is None


def test_clean_latex_matrix_and_doubled_symbols():
    cleaned = clean_latex_artifacts("space​here ∈ ∈ and Rn × ×d matrix")
    assert "​" not in cleaned
    assert "∈ ∈" not in cleaned
    assert "R^{n×d}" in cleaned


# ── _extract_year (lines 423-426) ──
def test_extract_year_int_string_value():
    assert _extract_year("2017", None) == 2017


def test_extract_year_mines_year_from_date_string():
    """A non-int date-like string → int() raises → regex mines the 4-digit year (424-426)."""
    assert _extract_year("Published: 2019-03-11", None) == 2019


def test_extract_year_unparseable_string_then_arxiv_fallback():
    """A string with no year at all → falls through to the arxiv-id YYMM heuristic."""
    assert _extract_year("no year here", "2401.00001") == 2024


def test_extract_year_old_century_from_arxiv():
    """yy >= 50 → 1900 + yy (e.g. a fabricated '9901' id → 1999)."""
    assert _extract_year(None, "9901.12345") == 1999


def test_extract_year_none_everywhere():
    assert _extract_year(None, None) is None


# ── build_acronym_dict (single-word full form is rejected) ──
def test_build_acronym_dict_rejects_single_word():
    # 'Test (T)' — only one word before the paren → not recorded; two words → recorded.
    out = build_acronym_dict("Foo (F) and Maximum Inner Product Search (MIPS)")
    assert "MIPS" in out
    assert "F" not in out


# ── parse_paper metadata edge cases (lines 488, 497, 503, 513, 517, 525-527) ──
def test_parse_paper_title_list_and_authors_list():
    """title given as a list (line 488) and authors as a non-string iterable (line 497)."""
    doc = parse_paper(
        "## Method\nbody text here for the section body to be retained nicely.",
        title=["First Title", "ignored"],
        authors=["Ada Lovelace", "  ", "Alan Turing"],
    )
    assert doc.title == "First Title"
    assert doc.authors == ["Ada Lovelace", "Alan Turing"]  # blank dropped


def test_parse_paper_authors_string_categories_list_arxiv_docid():
    """authors as a comma-separated string (line 495), categories as a non-string list
    (line 505), and an arxiv_id → doc_id arxiv_<id> with slashes flattened (line 511)."""
    doc = parse_paper(
        "body text",
        title="T",
        authors="Ada Lovelace, Alan Turing",
        categories=["cs.CL", "  ", "cs.LG"],
        arxiv_id="cs/0001",
    )
    assert doc.authors == ["Ada Lovelace", "Alan Turing"]
    assert doc.categories == ["cs.CL", "cs.LG"]  # blank dropped
    assert doc.doc_id == "arxiv_cs_0001"  # slash flattened


def test_parse_paper_categories_string_split():
    """categories as a comma-separated string (line 503) → list + topic from first."""
    doc = parse_paper("body", title="T", categories="cs.CL, cs.LG ,  ")
    assert doc.categories == ["cs.CL", "cs.LG"]
    assert doc.topic == "cs.CL"


def test_parse_paper_doc_id_from_doi():
    """No arxiv_id but a doi → doc_id is doi_<doi> with slashes flattened (line 513)."""
    doc = parse_paper("body", title="T", doi="10.1000/xyz/abc")
    assert doc.doc_id == "doi_10.1000_xyz_abc"
    assert doc.arxiv_id is None


def test_parse_paper_doc_id_from_url():
    """No arxiv_id / doi but a url → doc_id is the url (line 515)."""
    doc = parse_paper("body", title="T", url="https://example.com/p")
    assert doc.doc_id == "https://example.com/p"


def test_parse_paper_doc_id_unknown():
    """No identifiers at all → doc_id 'unknown' (line 517)."""
    doc = parse_paper("body", title="T")
    assert doc.doc_id == "unknown"


def test_parse_paper_pdf_fallback_when_markdown_flat():
    """Plain (no markdown #) PDF-style text where markdown extraction finds no real
    structure → the PDF extractor fallback kicks in (lines 523-527)."""
    text = (
        "Leading paragraph that is comfortably longer than thirty characters for keeping.\n"
        "1. Introduction\n"
        "This introduction body comfortably exceeds the thirty character retention floor.\n"
        "2. Method\n"
        "The method body is likewise long enough to be retained as a real PDF section here.\n"
    )
    doc = parse_paper(text, title="PDF Paper")
    # markdown extract_sections would only see {preamble} (no '#'); the PDF fallback adds
    # genuine named sections.
    assert "introduction" in doc.sections
    assert "method" in doc.sections
    assert _has_real_sections(doc.sections)


# ── _has_real_sections (line 554) ──
def test_has_real_sections_empty_false():
    assert _has_real_sections({}) is False


def test_has_real_sections_only_synthetic_false():
    assert _has_real_sections({"preamble": "x"}) is False
    assert _has_real_sections({"full_text": "x"}) is False


def test_has_real_sections_named_true():
    assert _has_real_sections({"method": "x"}) is True
