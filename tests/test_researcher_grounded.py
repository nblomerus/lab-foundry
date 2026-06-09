"""Unit tests for the library-grounded Researcher engine (agents.researcher.grounded).

Covers the read-only shadow path end-to-end with everything external mocked:
  - _task_context  : tasks/claims join + claim_goals lookup (with/without claim_id & goal; None)
  - _search_queries: LLM query extraction + the JSON-parse-failure fallback to the direction
  - investigate_task: query extraction → retrieve per-query merged/deduped → answer_question →
                      verdict synthesis → GroundedFinding (happy path, emit threading, no-evidence,
                      missing task, malformed verdict JSON, blocker fields)
  - grade_finding  : grounded fraction = cited titles resolving to retrieved titles + unresolved list

No real Postgres / Neo4j / Ollama / DeepSeek / network — see tests/_helpers.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.researcher.grounded as grounded
from agents.researcher.grounded import (
    GroundedFinding,
    _search_queries,
    _task_context,
    grade_finding,
    investigate_task,
)
from tests._helpers import ScriptedPool, make_state, patch_chain

# ── canned LLM payloads ───────────────────────────────────────────────────────
_QUERIES_JSON = json.dumps({"queries": ["gaussian process kernels", "sparse GP approximation", "deep GPs"]})

_FINDING_JSON = json.dumps(
    {
        "verdict": "supports",
        "blocker": "none",
        "confidence": 0.8,
        "summary": "The corpus backs the expectation.",
        "key_evidence": ["Paper A", "Paper B"],
        "kill_condition_check": "nothing trips the kill-condition",
        "gaps": ["scaling beyond 1M points"],
        "acquire_queries": [],
        "next_step": "run the benchmark",
    }
)


# ── fixtures / builders ───────────────────────────────────────────────────────
def _ref(document_id, title, *, trust_tier="certified", snippet="snip"):
    return SimpleNamespace(document_id=document_id, title=title, trust_tier=trust_tier, snippet=snippet)


def _mimir(answer="mimir says X", gaps=None, citations=None):
    return SimpleNamespace(answer=answer, gaps=gaps or [], citations=citations or [])


def _task_rules(
    *,
    task_id=1,
    claim_id=7,
    direction="GP variants beat baselines",
    expectation="lower RMSE",
    kill_condition="no improvement",
):
    """ScriptedPool rules for _task_context: the tasks/claims row, then the claim_goals row."""
    task_row = {
        "id": task_id,
        "task_type": "survey",
        "description": "synthesize 5 falsifiable hypotheses about novel GP variants",
        "claim_id": claim_id,
        "direction": direction,
    }
    goal_row = {"expectation": expectation, "kill_condition": kill_condition}
    return [
        ("FROM tasks t LEFT JOIN claims", [task_row]),
        ("FROM claim_goals", [goal_row]),
    ]


def _finding(**over):
    base = dict(
        verdict="inconclusive",
        blocker="none",
        confidence=0.5,
        summary="s",
        key_evidence=[],
        kill_condition_check="k",
        gaps=[],
        acquire_queries=[],
        next_step="n",
    )
    base.update(over)
    return GroundedFinding(**base)


def _patch_mimir(monkeypatch, *, refs=None, mimir=None):
    """Patch retrieve + answer_question ON the grounded module. retrieve returns `refs` for any query."""
    refs = refs if refs is not None else [_ref(1, "Paper A"), _ref(2, "Paper B")]
    retrieve = AsyncMock(return_value=refs)
    aq = AsyncMock(return_value=mimir if mimir is not None else _mimir())
    monkeypatch.setattr(grounded, "retrieve", retrieve)
    monkeypatch.setattr(grounded, "answer_question", aq)
    return retrieve, aq


# ══════════════════════════════════════════════════════════════════════════════
# _task_context
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_task_context_with_claim_and_goal():
    pool = ScriptedPool(rules=_task_rules())
    ctx = await _task_context(pool, 1)
    assert ctx["task_id"] == 1
    assert ctx["task_type"] == "survey"
    assert ctx["claim_id"] == 7
    assert ctx["direction"] == "GP variants beat baselines"
    assert ctx["expectation"] == "lower RMSE"
    assert ctx["kill_condition"] == "no improvement"
    # the goal query DID run (claim_id present)
    assert any("FROM claim_goals" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_task_context_missing_task_returns_none():
    pool = ScriptedPool(rules=[])  # unmatched fetchrow → None
    assert await _task_context(pool, 999) is None


@pytest.mark.asyncio
async def test_task_context_no_claim_skips_goal_and_defaults():
    # claim_id None → no claim_goals query, and direction/expectation/kill fall to defaults
    rules = [
        (
            "FROM tasks t LEFT JOIN claims",
            [{"id": 5, "task_type": "analyze", "description": "d", "claim_id": None, "direction": None}],
        ),
    ]
    pool = ScriptedPool(rules=rules)
    ctx = await _task_context(pool, 5)
    assert ctx["claim_id"] is None
    assert ctx["direction"] == "(no direction linked)"
    assert ctx["expectation"] == "(no explicit expectation)"
    assert ctx["kill_condition"] == "(no explicit kill-condition)"
    assert not any("FROM claim_goals" in sql for _, sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_task_context_claim_but_no_goal_row_uses_defaults():
    # claim_id present but claim_goals returns nothing → goal is None → defaults
    rules = [
        (
            "FROM tasks t LEFT JOIN claims",
            [{"id": 5, "task_type": "compare", "description": "d", "claim_id": 9, "direction": "dir"}],
        ),
        # no claim_goals rule → unmatched fetchrow returns None → goal is None
    ]
    pool = ScriptedPool(rules=rules)
    ctx = await _task_context(pool, 5)
    assert ctx["claim_id"] == 9
    assert ctx["direction"] == "dir"
    assert ctx["expectation"] == "(no explicit expectation)"
    assert ctx["kill_condition"] == "(no explicit kill-condition)"


@pytest.mark.asyncio
async def test_task_context_goal_present_but_blank_fields_use_defaults():
    # goal row exists but its fields are None/empty → `or` defaults kick in
    rules = [
        (
            "FROM tasks t LEFT JOIN claims",
            [{"id": 1, "task_type": "survey", "description": "d", "claim_id": 7, "direction": "dir"}],
        ),
        ("FROM claim_goals", [{"expectation": None, "kill_condition": ""}]),
    ]
    pool = ScriptedPool(rules=rules)
    ctx = await _task_context(pool, 1)
    assert ctx["expectation"] == "(no explicit expectation)"
    assert ctx["kill_condition"] == "(no explicit kill-condition)"


# ══════════════════════════════════════════════════════════════════════════════
# _search_queries
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_search_queries_happy_extracts_and_caps_to_four(monkeypatch):
    many = json.dumps({"queries": ["q1", "q2", "q3", "q4", "q5", "q6"]})
    calls = patch_chain(monkeypatch, grounded, content=many)
    ctx = {"description": "the task instruction", "direction": "the direction"}
    qs = await _search_queries(ctx, model="m")
    assert qs == ["q1", "q2", "q3", "q4"]  # capped at 4
    # the model was asked with the task + direction, not the raw instruction-only
    (messages, kw) = calls[0]
    assert kw["step_name"] == "queries"
    assert "the task instruction" in messages[1]["content"]


@pytest.mark.asyncio
async def test_search_queries_drops_blank_and_nonstring(monkeypatch):
    payload = json.dumps({"queries": ["  good  ", "  ", "alsogood", 5, None]})
    patch_chain(monkeypatch, grounded, content=payload)
    ctx = {"description": "d", "direction": "the direction"}
    qs = await _search_queries(ctx, model="m")
    assert qs == ["good", "alsogood"]  # stripped, blanks/non-strings removed


@pytest.mark.asyncio
async def test_search_queries_empty_list_falls_back_to_direction(monkeypatch):
    # valid JSON but no usable queries → `qs[:4] or [direction[:120]]`
    patch_chain(monkeypatch, grounded, content=json.dumps({"queries": []}))
    ctx = {"description": "d", "direction": "X" * 200}
    qs = await _search_queries(ctx, model="m")
    assert qs == ["X" * 120]


@pytest.mark.asyncio
async def test_search_queries_json_parse_failure_falls_back(monkeypatch):
    # malformed JSON → except branch → fall back to direction[:120]
    patch_chain(monkeypatch, grounded, content="not json at all {")
    ctx = {"description": "desc", "direction": "the chosen direction"}
    qs = await _search_queries(ctx, model="m")
    assert qs == ["the chosen direction"]


@pytest.mark.asyncio
async def test_search_queries_fallback_uses_description_when_no_direction(monkeypatch):
    # except branch, direction empty → `direction[:120] or description[:120]`
    patch_chain(monkeypatch, grounded, content="broken")
    ctx = {"description": "fallback description", "direction": ""}
    qs = await _search_queries(ctx, model="m")
    assert qs == ["fallback description"]


# ══════════════════════════════════════════════════════════════════════════════
# investigate_task
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_investigate_task_missing_returns_none(monkeypatch):
    pool = ScriptedPool(rules=[])  # unmatched fetchrow → None → task not found
    state = make_state(pool)
    _patch_mimir(monkeypatch)
    patch_chain(monkeypatch, grounded, content=_FINDING_JSON)
    assert await investigate_task(state, 999) is None


@pytest.mark.asyncio
async def test_investigate_task_happy_path(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    retrieve, aq = _patch_mimir(
        monkeypatch,
        refs=[_ref(1, "Paper A"), _ref(2, "Paper B")],
        mimir=_mimir(answer="established X", gaps=["g1"]),
    )
    # two LLM calls: first → queries, second → finding
    calls = patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    result = await investigate_task(state, 1)
    assert result is not None
    ctx, refs, mimir, finding = result

    assert ctx["queries"] == ["gaussian process kernels", "sparse GP approximation", "deep GPs"]
    assert [r.document_id for r in refs] == [1, 2]
    assert mimir.answer == "established X"
    assert isinstance(finding, GroundedFinding)
    assert finding.verdict == "supports"
    assert finding.key_evidence == ["Paper A", "Paper B"]

    # one retrieve per query (3 queries)
    assert retrieve.await_count == 3
    # emit=False → answer_question got state=None
    _, aq_kw = aq.await_args
    assert aq_kw["state"] is None
    assert aq_kw["asker"] == "researcher"
    # the finding prompt included Mimir's flagged gaps
    finding_user = calls[1][0][1]["content"]
    assert "Gaps Mimir flags: g1" in finding_user


@pytest.mark.asyncio
async def test_investigate_task_dedupes_refs_across_queries(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    # every query returns the SAME doc ids → must dedupe to 2 refs total
    shared = [_ref(1, "Paper A"), _ref(2, "Paper B")]
    retrieve = AsyncMock(return_value=shared)
    monkeypatch.setattr(grounded, "retrieve", retrieve)
    monkeypatch.setattr(grounded, "answer_question", AsyncMock(return_value=_mimir()))
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    _, refs, _, _ = await investigate_task(state, 1)
    assert [r.document_id for r in refs] == [1, 2]  # deduped despite 3 queries × 2 dupes


@pytest.mark.asyncio
async def test_investigate_task_caps_refs_at_twelve(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    # query 1 returns 20 unique docs → refs capped to 12
    big = [_ref(i, f"Paper {i}") for i in range(20)]
    retrieve = AsyncMock(side_effect=[big, [], []])
    monkeypatch.setattr(grounded, "retrieve", retrieve)
    monkeypatch.setattr(grounded, "answer_question", AsyncMock(return_value=_mimir()))
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    _, refs, _, _ = await investigate_task(state, 1)
    assert len(refs) == 12


@pytest.mark.asyncio
async def test_investigate_task_no_evidence_uses_placeholder(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    monkeypatch.setattr(grounded, "retrieve", AsyncMock(return_value=[]))  # no refs at all
    monkeypatch.setattr(grounded, "answer_question", AsyncMock(return_value=_mimir(gaps=[])))
    calls = patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    _, refs, _, _ = await investigate_task(state, 1)
    assert refs == []
    finding_user = calls[1][0][1]["content"]
    assert "(no corpus evidence retrieved)" in finding_user
    # no gaps → the 'Gaps Mimir flags' line is absent
    assert "Gaps Mimir flags" not in finding_user


@pytest.mark.asyncio
async def test_investigate_task_uses_title_placeholder_and_snippet(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    refs = [_ref(1, None, snippet="a snippet of evidence")]  # title None → 'untitled'
    monkeypatch.setattr(grounded, "retrieve", AsyncMock(return_value=refs))
    monkeypatch.setattr(grounded, "answer_question", AsyncMock(return_value=_mimir()))
    calls = patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    await investigate_task(state, 1)
    finding_user = calls[1][0][1]["content"]
    assert "untitled" in finding_user
    assert "a snippet of evidence" in finding_user


@pytest.mark.asyncio
async def test_investigate_task_emit_threads_state(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    _, aq = _patch_mimir(monkeypatch)
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, _FINDING_JSON])

    await investigate_task(state, 1, emit=True)
    _, aq_kw = aq.await_args
    assert aq_kw["state"] is state  # emit=True → live path threads the real state


@pytest.mark.asyncio
async def test_investigate_task_malformed_verdict_json_raises(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    _patch_mimir(monkeypatch)
    # queries ok, but finding JSON is malformed → model_validate_json raises
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, "this is not json"])
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        await investigate_task(state, 1)


@pytest.mark.asyncio
async def test_investigate_task_blocker_thin_corpus_fields(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    _patch_mimir(monkeypatch)
    finding_json = json.dumps(
        {
            "verdict": "inconclusive",
            "blocker": "thin_corpus",
            "confidence": 0.2,
            "summary": "not enough papers",
            "key_evidence": [],
            "kill_condition_check": "untested",
            "gaps": ["benchmark numbers"],
            "acquire_queries": ["GP scaling benchmark", "inducing point selection"],
            "next_step": "acquire",
        }
    )
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, finding_json])

    _, _, _, finding = await investigate_task(state, 1)
    assert finding.verdict == "inconclusive"
    assert finding.blocker == "thin_corpus"
    assert finding.acquire_queries == ["GP scaling benchmark", "inducing point selection"]


@pytest.mark.asyncio
async def test_investigate_task_fenced_finding_json_is_stripped(monkeypatch):
    pool = ScriptedPool(rules=_task_rules())
    state = make_state(pool)
    _patch_mimir(monkeypatch)
    fenced = f"```json\n{_FINDING_JSON}\n```"
    patch_chain(monkeypatch, grounded, content=[_QUERIES_JSON, fenced])

    _, _, _, finding = await investigate_task(state, 1)
    assert finding.verdict == "supports"


# ══════════════════════════════════════════════════════════════════════════════
# grade_finding
# ══════════════════════════════════════════════════════════════════════════════
def test_grade_finding_all_resolve():
    refs = [_ref(1, "Paper A"), _ref(2, "Paper B")]
    finding = _finding(verdict="supports", key_evidence=["Paper A", "paper b"])  # case-insensitive
    g = grade_finding(finding, refs)
    assert g["verdict_valid"] is True
    assert g["n_cited"] == 2
    assert g["n_resolved"] == 2
    assert g["grounded"] == 1.0
    assert g["unresolved"] == []


def test_grade_finding_partial_resolution_and_unresolved_list():
    refs = [_ref(1, "Paper A")]
    finding = _finding(key_evidence=["Paper A", "Ghost Paper"])  # one invented
    g = grade_finding(finding, refs)
    assert g["n_cited"] == 2
    assert g["n_resolved"] == 1
    assert g["grounded"] == 0.5
    assert g["unresolved"] == ["Ghost Paper"]


def test_grade_finding_no_citations_grounded_zero():
    refs = [_ref(1, "Paper A")]
    finding = _finding(key_evidence=[])
    g = grade_finding(finding, refs)
    assert g["n_cited"] == 0
    assert g["grounded"] == 0.0
    assert g["unresolved"] == []


def test_grade_finding_blank_citations_ignored():
    refs = [_ref(1, "Paper A")]
    finding = _finding(key_evidence=["  ", "", "Paper A"])  # blanks dropped from `cited`
    g = grade_finding(finding, refs)
    assert g["n_cited"] == 1  # only "Paper A"
    assert g["n_resolved"] == 1
    assert g["grounded"] == 1.0


def test_grade_finding_invalid_verdict_flagged():
    refs = [_ref(1, "Paper A")]
    finding = _finding(verdict="contradicts")  # valid here; flip via model bypass below
    g = grade_finding(finding, refs)
    assert g["verdict_valid"] is True
    # construct an out-of-vocab verdict by mutating the validated instance
    finding.verdict = "maybe"
    assert grade_finding(finding, refs)["verdict_valid"] is False


def test_grade_finding_ref_without_title_ignored_in_titles():
    refs = [_ref(1, None), _ref(2, "Paper B")]  # title None skipped from title set
    finding = _finding(key_evidence=["Paper B", "Paper A"])
    g = grade_finding(finding, refs)
    assert g["n_resolved"] == 1
    assert g["unresolved"] == ["Paper A"]


def test_grade_finding_unresolved_capped_at_five():
    refs = [_ref(1, "Real")]
    finding = _finding(key_evidence=[f"Ghost {i}" for i in range(8)])
    g = grade_finding(finding, refs)
    assert len(g["unresolved"]) == 5  # capped
