"""Unit tests for Ariadne's shadow-mode deliberation loop (agents.ariadne.loop).

Everything external is mocked: the LLM chain (_chain_complete), Mimir's answer_question,
hybrid retrieval (corpus_search), the field-model brief (read_field_brief), and the Neo4j
driver (_get_driver). No real Postgres/Neo4j/Ollama/DeepSeek/network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.ariadne import loop
from tests._helpers import FakeNeoDriver, ScriptedPool, make_state, patch_chain

# ── canned AriadneOutput the parser expects ────────────────────────────────────
_VALID_SCORES = {
    "novelty": 4,
    "impact": 4,
    "feasibility": 3,
    "evidence_availability": 4,
    "paper_potential": 3,
    "reviewer_interest": 4,
    "technical_depth": 3,
    "differentiation": 4,
    "cost_efficiency": 5,
    "lab_alignment": 4,
    "rationale": "emerging gap, light to run",
}

_VALID_OUTPUT = {
    "mission_frame": "Make retrieval trustworthy under distribution shift.",
    "directions": [
        {
            "title": "Trust-weighted RRF",
            "statement": "Attack stale retrieval via trust-decayed RRF.",
            "stakes": "RAG engineers decide whether to decay retrieval trust over time.",
            "novelty_rationale": "No prior work decays trust over time.",
            "grounded_in": ["A Survey of Hybrid Retrieval"],
            "scores": _VALID_SCORES,
            "claim_goals": [
                {
                    "expectation": "R@20 improves on the stale slice",
                    "kill_condition": "no lift after tuning",
                    "novelty_target": "time-decayed trust",
                    "next_milestone": "ablation on 1k queries",
                    "priority_hint": "high",
                }
            ],
            "kill_conditions": ["no measurable lift"],
            "reviewer_risks": ["weak baselines"],
        }
    ],
    "novelty_risks": ["saturation in dense retrieval"],
    "requests": [{"paper": "Original RRF paper", "arxiv_id": "2009.12345", "why": "foundational"}],
    "reflection": "Uncertain whether trust signal generalizes.",
}


def _valid_json() -> str:
    return json.dumps(_VALID_OUTPUT)


def _chunk(
    title="A Survey of Hybrid Retrieval",
    source_url="https://arxiv.org/abs/2406.12345",
    trust_tier="certified",
    text="Hybrid retrieval fuses BM25 and dense scores via RRF.",
):
    return SimpleNamespace(
        document_id=1,
        title=title,
        source_url=source_url,
        trust_tier=trust_tier,
        text=text,
    )


def _mimir_answer(answer="Methods M connect to tasks T.", gaps=("trust-aware reranking",)):
    return SimpleNamespace(answer=answer, gaps=list(gaps))


# ════════════════════════════════════════════════════════════════════════════════
# _arxiv_tag
# ════════════════════════════════════════════════════════════════════════════════
def test_arxiv_tag_matches_abs_url():
    assert loop._arxiv_tag("https://arxiv.org/abs/2406.12345") == " [arxiv:2406.12345]"


def test_arxiv_tag_five_digit_id():
    assert loop._arxiv_tag("http://arxiv.org/abs/2310.01234") == " [arxiv:2310.01234]"


def test_arxiv_tag_no_match_returns_empty():
    assert loop._arxiv_tag("https://example.com/paper.pdf") == ""


def test_arxiv_tag_none_returns_empty():
    assert loop._arxiv_tag(None) == ""


# ════════════════════════════════════════════════════════════════════════════════
# _lesson_when — dict / json-text / bare / None
# ════════════════════════════════════════════════════════════════════════════════
def test_lesson_when_none_or_empty():
    assert loop._lesson_when(None) == ""
    assert loop._lesson_when("") == ""
    assert loop._lesson_when({}) == ""


def test_lesson_when_dict_with_when():
    assert loop._lesson_when({"when": "R@20 < 0.3"}) == "R@20 < 0.3"


def test_lesson_when_dict_without_when_key():
    assert loop._lesson_when({"foo": "bar"}) == ""


def test_lesson_when_json_text():
    assert loop._lesson_when('{"when": "eval is weak"}') == "eval is weak"


def test_lesson_when_bare_string_not_json():
    assert loop._lesson_when("just a plain condition") == "just a plain condition"


def test_lesson_when_non_dict_non_str_coerced():
    # a list survives json.loads but is not dict/str → str(v)
    assert loop._lesson_when([1, 2, 3]) == "[1, 2, 3]"


def test_lesson_when_json_scalar_number():
    # "5" parses to int 5 → falls through to str()
    assert loop._lesson_when("5") == "5"


# ════════════════════════════════════════════════════════════════════════════════
# _top_concepts — Neo4j happy path + failure branch
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_top_concepts_happy_path(monkeypatch):
    records = [{"name": "RAG", "papers": 42}, {"name": "RLHF", "papers": 30}]
    driver = FakeNeoDriver(on_run=lambda q, p: records)
    monkeypatch.setattr(loop, "_get_driver", AsyncMock(return_value=driver))
    out = await loop._top_concepts("METHOD", "USES")
    assert out == [("RAG", 42), ("RLHF", 30)]
    # the cypher carried label/rel and the limit param
    q, params = driver.sessions[0].queries[0]
    assert "METHOD" in q and "USES" in q
    assert params["limit"] == 18


@pytest.mark.asyncio
async def test_top_concepts_driver_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(loop, "_get_driver", AsyncMock(side_effect=RuntimeError("neo down")))
    assert await loop._top_concepts("TASK", "ADDRESSES") == []


# ════════════════════════════════════════════════════════════════════════════════
# recall_lessons
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_recall_lessons_pool_none():
    assert await loop.recall_lessons(None) == ""


@pytest.mark.asyncio
async def test_recall_lessons_query_failure_returns_empty():
    class _BoomPool:
        async def fetch(self, *a):
            raise RuntimeError("db down")

    assert await loop.recall_lessons(_BoomPool()) == ""


@pytest.mark.asyncio
async def test_recall_lessons_empty_rows():
    pool = ScriptedPool(rules=[("FROM lessons", [])])
    assert await loop.recall_lessons(pool) == ""


@pytest.mark.asyncio
async def test_recall_lessons_formats_with_and_without_condition():
    rows = [
        {"lesson_text": "Use strong baselines", "applies_when": {"when": "weak eval"}, "status": "active"},
        {"lesson_text": "Prefer light methods", "applies_when": None, "status": "probationary"},
    ]
    pool = ScriptedPool(rules=[("FROM lessons", rows)])
    out = await loop.recall_lessons(pool)
    assert out.startswith("## Standing lessons")
    assert "- [active] Use strong baselines (when: weak eval)" in out
    assert "- [probationary] Prefer light methods" in out
    # no trailing "(when: ...)" for the null condition row
    assert "Prefer light methods (when:" not in out


# ════════════════════════════════════════════════════════════════════════════════
# _mimir_brief
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_mimir_brief_pool_none_short_circuits(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(loop, "answer_question", called)
    block, gaps = await loop._mimir_brief("seed", None)
    assert block == "" and gaps == []
    called.assert_not_called()


@pytest.mark.asyncio
async def test_mimir_brief_happy_path_with_anchors_and_gaps(monkeypatch):
    rows = [{"concept_name": "agentic RAG"}, {"concept_name": "tool use"}]
    pool = ScriptedPool(rules=[("FROM field_model", rows)])
    captured = {}

    async def _aq(question, k, state, asker):
        captured["question"] = question
        captured["k"] = k
        captured["state"] = state
        captured["asker"] = asker
        return _mimir_answer(gaps=["trust-aware reranking", "stale-doc decay"])

    monkeypatch.setattr(loop, "answer_question", _aq)
    block, gaps = await loop._mimir_brief("Build a trustworthy retriever", pool, state="STATE")
    assert "Mimir's synthesis" in block
    assert "UNDER-EXPLORED GAPS" in block
    assert "- trust-aware reranking" in block
    assert gaps == ["trust-aware reranking", "stale-doc decay"]
    # anchors injected into the question, state threaded, asker tagged
    assert "agentic RAG, tool use" in captured["question"]
    assert captured["state"] == "STATE" and captured["asker"] == "ariadne" and captured["k"] == 8


@pytest.mark.asyncio
async def test_mimir_brief_no_anchors_when_field_model_query_fails(monkeypatch):
    class _PartialPool:
        async def fetch(self, *a):
            raise RuntimeError("no field_model table")

    captured = {}

    async def _aq(question, k, state, asker):
        captured["question"] = question
        return _mimir_answer(gaps=[])

    monkeypatch.setattr(loop, "answer_question", _aq)
    block, gaps = await loop._mimir_brief("seed problem", _PartialPool())
    # no anchors block injected
    assert "Focus on active/emerging areas" not in captured["question"]
    # gaps empty → no UNDER-EXPLORED section
    assert "Mimir's synthesis" in block
    assert "UNDER-EXPLORED GAPS" not in block
    assert gaps == []


@pytest.mark.asyncio
async def test_mimir_brief_answer_question_raises_returns_empty(monkeypatch):
    pool = ScriptedPool(rules=[("FROM field_model", [{"concept_name": "x"}])])

    async def _boom(*a, **k):
        raise RuntimeError("mimir offline")

    monkeypatch.setattr(loop, "answer_question", _boom)
    block, gaps = await loop._mimir_brief("seed", pool)
    assert block == "" and gaps == []


# ════════════════════════════════════════════════════════════════════════════════
# recall_prior_art
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_recall_prior_art_field_brief_path(monkeypatch):
    monkeypatch.setattr(loop, "corpus_search", AsyncMock(return_value=[_chunk()]))
    monkeypatch.setattr(loop, "read_field_brief", AsyncMock(return_value="## FIELD MODEL\nhot: RAG"))
    monkeypatch.setattr(loop, "answer_question", AsyncMock(return_value=_mimir_answer()))
    # field_model anchors + lessons reads go through the pool
    pool = ScriptedPool(
        rules=[
            ("FROM field_model", [{"concept_name": "agentic RAG"}]),
            (
                "FROM lessons",
                [{"lesson_text": "Use strong baselines", "applies_when": {"when": "weak eval"}, "status": "active"}],
            ),
        ]
    )
    text, gaps = await loop.recall_prior_art("seed", pool=pool, state="S")
    assert "Retrieved prior art" in text
    assert "[certified] A Survey of Hybrid Retrieval [arxiv:2406.12345]" in text
    assert "## FIELD MODEL" in text  # field brief, NOT the fallback landscape
    assert "Corpus concept landscape" not in text
    assert "Mimir's synthesis" in text
    assert "Standing lessons" in text
    assert gaps == ["trust-aware reranking"]


@pytest.mark.asyncio
async def test_recall_prior_art_fallback_top_concepts_path(monkeypatch):
    monkeypatch.setattr(loop, "corpus_search", AsyncMock(return_value=[_chunk()]))
    # field brief empty → fall through to _top_concepts
    monkeypatch.setattr(loop, "read_field_brief", AsyncMock(return_value=""))
    monkeypatch.setattr(loop, "answer_question", AsyncMock(return_value=_mimir_answer(gaps=[])))

    async def _top(label, rel, limit=18):
        return {"METHOD": [("RAG", 42)], "TASK": [("QA", 10)], "DATASET": []}[label]

    monkeypatch.setattr(loop, "_top_concepts", _top)
    pool = ScriptedPool(
        rules=[
            ("FROM field_model", []),
            ("FROM lessons", []),
        ]
    )
    text, gaps = await loop.recall_prior_art("seed", pool=pool, state=None)
    assert "Corpus concept landscape" in text
    assert "METHODS: RAG (42)" in text
    assert "TASKS: QA (10)" in text
    assert "DATASETS: (none extracted yet)" in text  # empty → placeholder
    assert gaps == []


@pytest.mark.asyncio
async def test_recall_prior_art_no_passages_pool_none(monkeypatch):
    monkeypatch.setattr(loop, "corpus_search", AsyncMock(return_value=[]))
    monkeypatch.setattr(loop, "answer_question", AsyncMock(return_value=_mimir_answer()))

    async def _top(label, rel, limit=18):
        return []

    monkeypatch.setattr(loop, "_top_concepts", _top)
    # pool=None → read_field_brief skipped (landscape ""), _mimir_brief short-circuits,
    # recall_lessons returns "" → no lessons section
    text, gaps = await loop.recall_prior_art("seed", pool=None, state=None)
    assert "(no passages retrieved)" in text
    assert "Corpus concept landscape" in text  # fallback landscape used
    assert "Mimir's synthesis" not in text
    assert "Standing lessons" not in text
    assert gaps == []


# ════════════════════════════════════════════════════════════════════════════════
# _deliberate — JSON parsing happy + malformed
# ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_deliberate_parses_valid_output(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    out = await loop._deliberate("seed", "agenda", "prior art", model="deepseek-v4-flash")
    assert out.mission_frame.startswith("Make retrieval")
    assert out.directions[0].title == "Trust-weighted RRF"
    assert out.directions[0].scores.cost_efficiency == 5
    assert out.requests[0].arxiv_id == "2009.12345"
    # the chain was asked with system+user and Ariadne's invocation metadata
    messages, kwargs = calls[0]
    assert messages[0]["role"] == "system"
    assert "Seed problem" in messages[1]["content"]
    assert kwargs["invocation_type"] == "ariadne.deliberate"
    assert kwargs["primary_model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_deliberate_strips_code_fences(monkeypatch):
    fenced = "```json\n" + _valid_json() + "\n```"
    patch_chain(monkeypatch, loop, content=fenced)
    out = await loop._deliberate("seed", "agenda", "art", model="m")
    assert out.mission_frame.startswith("Make retrieval")


@pytest.mark.asyncio
async def test_deliberate_malformed_json_raises(monkeypatch):
    patch_chain(monkeypatch, loop, content="this is not json at all")
    with pytest.raises(ValueError):
        await loop._deliberate("seed", "agenda", "art", model="m")


@pytest.mark.asyncio
async def test_deliberate_valid_json_missing_required_keys_raises(monkeypatch):
    # valid JSON but missing required AriadneOutput fields (directions/reflection)
    patch_chain(monkeypatch, loop, content='{"mission_frame": "x"}')
    with pytest.raises(ValueError):
        await loop._deliberate("seed", "agenda", "art", model="m")


# ════════════════════════════════════════════════════════════════════════════════
# run_shadow — the orchestrator
# ════════════════════════════════════════════════════════════════════════════════
def _wire_run_shadow(monkeypatch, *, claims=None, claims_raises=False):
    """Common stubs for run_shadow: corpus/field/mimir mocked; state with company_state + claims."""
    monkeypatch.setattr(loop, "corpus_search", AsyncMock(return_value=[_chunk()]))
    monkeypatch.setattr(loop, "read_field_brief", AsyncMock(return_value="## FIELD MODEL\nhot: RAG"))
    monkeypatch.setattr(loop, "answer_question", AsyncMock(return_value=_mimir_answer()))
    pool = ScriptedPool(
        rules=[
            ("FROM field_model", [{"concept_name": "agentic RAG"}]),
            ("FROM lessons", []),
        ]
    )
    state = make_state(pool=pool)
    state.get_company_state.return_value = SimpleNamespace(
        problem_statement="Make retrieval trustworthy.",
        stance="Demand a real decision changes.",
        success_criterion="A reproducible, decision-changing finding.",
    )
    if claims_raises:
        state.get_active_claims.side_effect = RuntimeError("claims read failed")
    else:
        state.get_active_claims.return_value = claims if claims is not None else []
    # First-party experiment/finding context (best-effort blocks) — empty by default.
    state.get_recent_experiment_notes_for_claims.return_value = []
    state.get_recent_findings.return_value = []
    return state


@pytest.mark.asyncio
async def test_run_shadow_happy_path_no_focus(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims=[])
    out = await loop.run_shadow(state)
    assert out.mission_frame.startswith("Make retrieval")
    user = calls[0][0][1]["content"]
    assert "Make retrieval trustworthy." in user
    assert "(empty — frame from scratch)" in user  # empty agenda
    # emit_conversation defaults False → Mimir called with state=None
    _args, kwargs = loop.answer_question.call_args
    assert kwargs["state"] is None


@pytest.mark.asyncio
async def test_run_shadow_injects_focus(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims=[])
    await loop.run_shadow(state, focus="latency tail of ts_rank")
    user = calls[0][0][1]["content"]
    assert "FOCUS THIS DELIBERATION ON: latency tail of ts_rank" in user


@pytest.mark.asyncio
async def test_run_shadow_surfaces_established_findings_globally(monkeypatch):
    # A finding survives a re-frame (read globally, not per-active-claim) → it reaches the next
    # deliberation so she builds beyond it instead of re-rolling.
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims=[])
    state.get_recent_findings.return_value = [
        {
            "direction_claim_id": 52,
            "headline": "Quantized GPs match XGBoost RMSE at 8-bit on tabular regression.",
            "claim_text": "...",
            "supported": "supported",
            "confidence": 0.7,
            "so_what": "A data scientist can ship a calibrated GP instead of XGBoost.",
            "n_experiments": 4,
        }
    ]
    await loop.run_shadow(state)
    user = calls[0][0][1]["content"]
    assert "Findings the lab has ESTABLISHED" in user
    assert "Quantized GPs match XGBoost RMSE at 8-bit" in user
    assert "[supported @0.70]" in user


@pytest.mark.asyncio
async def test_run_shadow_renders_agenda_from_claims(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    claims = [
        SimpleNamespace(claim_kind="mission", statement="Trustworthy retrieval"),
        SimpleNamespace(claim_kind="direction", statement="Trust-decayed RRF"),
        SimpleNamespace(statement="bare claim without kind"),  # falls back to 'hypothesis'
    ]
    state = _wire_run_shadow(monkeypatch, claims=claims)
    await loop.run_shadow(state)
    user = calls[0][0][1]["content"]
    assert "- [mission] Trustworthy retrieval" in user
    assert "- [direction] Trust-decayed RRF" in user
    assert "- [hypothesis] bare claim without kind" in user


@pytest.mark.asyncio
async def test_run_shadow_claims_read_failure_falls_back_to_empty(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims_raises=True)
    await loop.run_shadow(state)
    user = calls[0][0][1]["content"]
    assert "(empty — frame from scratch)" in user


@pytest.mark.asyncio
async def test_run_shadow_emit_conversation_threads_state(monkeypatch):
    patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims=[])
    await loop.run_shadow(state, emit_conversation=True)
    # state threaded into Mimir so the conversation emits live
    _args, kwargs = loop.answer_question.call_args
    assert kwargs["state"] is state


@pytest.mark.asyncio
async def test_run_shadow_uses_explicit_model(monkeypatch):
    calls = patch_chain(monkeypatch, loop, content=_valid_json())
    state = _wire_run_shadow(monkeypatch, claims=[])
    await loop.run_shadow(state, model="custom-model")
    assert calls[0][1]["primary_model"] == "custom-model"
