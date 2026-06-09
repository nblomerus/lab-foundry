"""Unit tests for Ariadne's predicate-grading of shadow output (agents.ariadne.grade).

grade.py turns the readiness plan's advisory gate into machine-checkable predicates:
schema validity, well-formed claim goals, grounded directions, well-formed scores, and
citation resolution against the corpus. The one external seam is corpus_search (the
anti-hallucination "does this citation resolve to a real doc" lookup) — it is mocked on
the module. grade_reflection is pure. No real Postgres / Neo4j / Ollama / DeepSeek / network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.ariadne import grade
from agents.ariadne.schemas import (
    AriadneOutput,
    ClaimGoal,
    DecisionScores,
    Direction,
    DirectionVerdict,
    ReflectionOutput,
    StrategicLesson,
)

# ── builders for the canned (LLM-graded) outputs ───────────────────────────────
_VALID_SCORES = {
    "novelty": 4,
    "feasibility": 3,
    "evidence_availability": 4,
    "paper_potential": 3,
    "reviewer_interest": 4,
    "technical_depth": 3,
    "differentiation": 4,
    "cost_efficiency": 5,
    "lab_alignment": 4,
    "rationale": "emerging gap",
}


def _scores(**over) -> DecisionScores:
    base = dict(_VALID_SCORES)
    base.update(over)
    return DecisionScores(**base)


def _goal(expectation="R@20 improves", kill_condition="no lift after tuning") -> ClaimGoal:
    return ClaimGoal(expectation=expectation, kill_condition=kill_condition)


_DEFAULT = object()


def _direction(
    *,
    title="Trust-weighted RRF",
    novelty_rationale="No prior work decays trust over time.",
    grounded_in=("A Survey of Hybrid Retrieval",),
    scores=_DEFAULT,
    claim_goals=None,
) -> Direction:
    return Direction(
        title=title,
        statement="Attack stale retrieval via trust-decayed RRF.",
        novelty_rationale=novelty_rationale,
        grounded_in=list(grounded_in),
        scores=_scores() if scores is _DEFAULT else scores,
        claim_goals=list(claim_goals) if claim_goals is not None else [_goal()],
    )


def _output(directions, *, mission_frame="Make retrieval trustworthy.", reflection="Uncertain.") -> AriadneOutput:
    return AriadneOutput(mission_frame=mission_frame, directions=directions, reflection=reflection)


def _chunk(title="A Survey of Hybrid Retrieval"):
    return SimpleNamespace(title=title)


def _patch_search(monkeypatch, *, chunks=None, side_effect=None):
    """Mock corpus_search on the grade module. `chunks` is what every call returns."""
    if side_effect is not None:
        monkeypatch.setattr(grade, "corpus_search", AsyncMock(side_effect=side_effect))
    else:
        monkeypatch.setattr(grade, "corpus_search", AsyncMock(return_value=list(chunks or [])))


# ════════════════════════════════════════════════════════════════════════════════
# _toks — tokenizer
# ════════════════════════════════════════════════════════════════════════════════
def test_toks_drops_stopwords_and_single_chars():
    assert grade._toks("A Survey of the Hybrid Retrieval") == {"survey", "hybrid", "retrieval"}


def test_toks_lowercases_and_keeps_alphanumeric():
    assert grade._toks("GPT4 BERT-base") == {"gpt4", "bert", "base"}


def test_toks_none_and_empty():
    assert grade._toks(None) == set()
    assert grade._toks("") == set()


def test_toks_all_stopwords_returns_empty():
    assert grade._toks("the a an of for") == set()


# ════════════════════════════════════════════════════════════════════════════════
# _resolves — citation → corpus title containment
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_resolves_too_few_tokens_short_circuits(monkeypatch):
    search = AsyncMock(return_value=[_chunk()])
    monkeypatch.setattr(grade, "corpus_search", search)
    # only one content token after stopword/length filtering → never searches
    assert await grade._resolves("the a survey") is False
    search.assert_not_called()


@pytest.mark.asyncio
async def test_resolves_exact_title_match(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk("A Survey of Hybrid Retrieval")])
    assert await grade._resolves("A Survey of Hybrid Retrieval") is True


@pytest.mark.asyncio
async def test_resolves_citation_covers_doc_title(monkeypatch):
    # verbose citation "title — section": doc title mostly contained in the citation
    _patch_search(monkeypatch, chunks=[_chunk("Hybrid Retrieval")])
    assert await grade._resolves("Hybrid Retrieval — Section 3 Experiments and Ablations") is True


@pytest.mark.asyncio
async def test_resolves_no_overlap_returns_false(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk("Quantum Error Correction Codes")])
    assert await grade._resolves("Trust Decayed Reciprocal Rank Fusion") is False


@pytest.mark.asyncio
async def test_resolves_skips_chunks_with_empty_title(monkeypatch):
    # first chunk has no usable title (skipped via continue), no other chunk → unresolved
    _patch_search(monkeypatch, chunks=[_chunk(""), _chunk(None)])
    assert await grade._resolves("Trust Decayed Reciprocal Rank Fusion") is False


@pytest.mark.asyncio
async def test_resolves_corpus_search_raises_returns_false(monkeypatch):
    _patch_search(monkeypatch, side_effect=RuntimeError("corpus down"))
    assert await grade._resolves("A Survey of Hybrid Retrieval") is False


@pytest.mark.asyncio
async def test_resolves_no_chunks_returns_false(monkeypatch):
    _patch_search(monkeypatch, chunks=[])
    assert await grade._resolves("A Survey of Hybrid Retrieval") is False


# ════════════════════════════════════════════════════════════════════════════════
# grade — the async entry point + GradeReport aggregation
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_grade_full_pass(monkeypatch):
    # every citation resolves, all predicates satisfied → passed
    _patch_search(monkeypatch, chunks=[_chunk("A Survey of Hybrid Retrieval")])
    out = _output([_direction(), _direction(), _direction()])
    rep = await grade.grade(out)
    assert rep.schema_valid is True
    assert rep.claim_goals_wellformed == 1.0
    assert rep.directions_grounded == 1.0
    assert rep.scores_wellformed == 1.0
    assert rep.citations_resolved == 1.0
    assert rep.n_citations == 3
    assert rep.unresolved == []
    assert rep.passed is True


@pytest.mark.asyncio
async def test_grade_fewer_than_three_directions_not_schema_valid(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    rep = await grade.grade(_output([_direction(), _direction()]))
    assert rep.schema_valid is False
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_blank_novelty_rationale_breaks_schema(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    dirs = [_direction(), _direction(), _direction(novelty_rationale="   ")]
    rep = await grade.grade(_output(dirs))
    assert rep.schema_valid is False
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_direction_without_goals_breaks_schema_and_partial_wellformed(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    dirs = [_direction(), _direction(), _direction(claim_goals=[])]
    rep = await grade.grade(_output(dirs))
    # one of three directions has no goals → schema invalid (each must have ≥1 goal)
    assert rep.schema_valid is False
    # cg_wf is computed over the 2 existing goals (both well-formed) → 1.0
    assert rep.claim_goals_wellformed == 1.0
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_claim_goals_partial_when_one_goal_malformed(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    bad_goal = _goal(expectation="   ")  # blank expectation
    dirs = [_direction(), _direction(), _direction(claim_goals=[_goal(), bad_goal])]
    rep = await grade.grade(_output(dirs))
    # 3 well-formed goals out of 4 total
    assert rep.claim_goals_wellformed == pytest.approx(0.75)
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_claim_goals_blank_kill_condition(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    bad = _goal(kill_condition="  ")
    dirs = [_direction(claim_goals=[bad]), _direction(), _direction()]
    rep = await grade.grade(_output(dirs))
    # 2 of 3 goals well-formed
    assert rep.claim_goals_wellformed == pytest.approx(2 / 3)
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_ungrounded_direction_lowers_grounded_and_citations(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    dirs = [_direction(), _direction(), _direction(grounded_in=[])]
    rep = await grade.grade(_output(dirs))
    # one direction has no grounded_in
    assert rep.directions_grounded == pytest.approx(2 / 3)
    assert rep.n_citations == 2
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_malformed_scores_lowers_scores_wellformed(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    # a direction whose scores object is None → not well-formed
    dirs = [_direction(scores=None), _direction(), _direction()]
    # Direction.scores defaults to None when explicitly passed None
    rep = await grade.grade(_output(dirs))
    assert rep.scores_wellformed == pytest.approx(2 / 3)
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_out_of_range_score_not_wellformed(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk()])
    dirs = [_direction(scores=_scores(novelty=9)), _direction(), _direction()]
    rep = await grade.grade(_output(dirs))
    assert rep.scores_wellformed == pytest.approx(2 / 3)
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_unresolved_citations_below_threshold(monkeypatch):
    # nothing resolves → cite_res 0.0, all citations land in unresolved
    _patch_search(monkeypatch, chunks=[_chunk("Totally Unrelated Title About Birds")])
    out = _output([_direction(), _direction(), _direction()])
    rep = await grade.grade(out)
    assert rep.citations_resolved == 0.0
    assert len(rep.unresolved) == 3
    assert rep.passed is False


@pytest.mark.asyncio
async def test_grade_citations_at_80_percent_passes(monkeypatch):
    # 4 of 5 citations resolve == 0.8 (the threshold) → still passes if all else OK
    resolving = "A Survey of Hybrid Retrieval"
    nonresolving = "Totally Unrelated Title About Birds"

    async def _search(title, k=6):
        return [_chunk(resolving)] if title == resolving else [_chunk("Quantum Error Correction")]

    monkeypatch.setattr(grade, "corpus_search", _search)
    dirs = [
        _direction(grounded_in=[resolving, resolving]),
        _direction(grounded_in=[resolving, resolving]),
        _direction(grounded_in=[nonresolving]),
    ]
    rep = await grade.grade(_output(dirs))
    assert rep.citations_resolved == pytest.approx(0.8)
    assert rep.unresolved == [nonresolving]
    assert rep.passed is True


@pytest.mark.asyncio
async def test_grade_unresolved_truncated_to_eight(monkeypatch):
    _patch_search(monkeypatch, chunks=[_chunk("Unrelated Bird Watching Guide")])
    # 10 distinct non-resolving citations spread across 3 directions
    cites = [f"Made Up Paper Number {i} On Trust Decay" for i in range(10)]
    dirs = [
        _direction(grounded_in=cites[:4]),
        _direction(grounded_in=cites[4:8]),
        _direction(grounded_in=cites[8:]),
    ]
    rep = await grade.grade(_output(dirs))
    assert rep.n_citations == 10
    assert len(rep.unresolved) == 8  # truncated for the report


@pytest.mark.asyncio
async def test_grade_empty_directions_zero_metrics(monkeypatch):
    # AriadneOutput requires directions, but an empty list is structurally allowed here
    _patch_search(monkeypatch, chunks=[_chunk()])
    rep = await grade.grade(_output([]))
    assert rep.schema_valid is False
    assert rep.claim_goals_wellformed == 0.0
    assert rep.directions_grounded == 0.0
    assert rep.scores_wellformed == 0.0
    assert rep.citations_resolved == 0.0
    assert rep.n_citations == 0
    assert rep.passed is False


# ════════════════════════════════════════════════════════════════════════════════
# grade_reflection — pure reflection grader
# ════════════════════════════════════════════════════════════════════════════════
def _verdict(claim_id=1, assessment="advance") -> DirectionVerdict:
    return DirectionVerdict(claim_id=claim_id, assessment=assessment, reason="field shifted")


def _reflection(verdicts, *, focus="Emphasize trust-aware retrieval.", lessons=None) -> ReflectionOutput:
    return ReflectionOutput(
        portfolio_assessment="Standing agenda still apt.",
        verdicts=list(verdicts),
        lessons=list(lessons) if lessons is not None else [],
        reprioritized_focus=focus,
    )


def test_grade_reflection_all_valid_passes():
    out = _reflection(
        [_verdict(1, "advance"), _verdict(2, "pivot")],
        lessons=[StrategicLesson(lesson="Use strong baselines")],
    )
    rep = grade.grade_reflection(out, valid_ids=[1, 2, 3])
    assert rep.verdicts_valid == 1.0
    assert rep.n_verdicts == 2
    assert rep.n_lessons == 1
    assert rep.invalid_refs == []
    assert rep.passed is True


def test_grade_reflection_unknown_id_is_invalid():
    out = _reflection([_verdict(1, "advance"), _verdict(99, "retire")])
    rep = grade.grade_reflection(out, valid_ids=[1, 2])
    assert rep.verdicts_valid == pytest.approx(0.5)
    assert rep.invalid_refs == [99]
    assert rep.passed is False


def test_grade_reflection_bad_assessment_is_invalid():
    out = _reflection([_verdict(1, "frobnicate")])
    rep = grade.grade_reflection(out, valid_ids=[1])
    assert rep.verdicts_valid == 0.0
    assert rep.invalid_refs == [1]
    assert rep.passed is False


def test_grade_reflection_no_verdicts_fails():
    out = _reflection([])
    rep = grade.grade_reflection(out, valid_ids=[1, 2])
    assert rep.verdicts_valid == 0.0
    assert rep.n_verdicts == 0
    assert rep.passed is False


def test_grade_reflection_blank_focus_fails():
    out = _reflection([_verdict(1, "advance")], focus="   ")
    rep = grade.grade_reflection(out, valid_ids=[1])
    assert rep.verdicts_valid == 1.0
    assert rep.passed is False  # blank reprioritized_focus


def test_grade_reflection_invalid_refs_truncated_to_eight():
    verdicts = [_verdict(claim_id=1000 + i, assessment="advance") for i in range(10)]
    out = _reflection(verdicts)
    rep = grade.grade_reflection(out, valid_ids=[1])
    assert rep.n_verdicts == 10
    assert len(rep.invalid_refs) == 8
