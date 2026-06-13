"""Unit tests for the novelty agent — the independent adjudicator. Everything external is
mocked (no corpus search, no LLM, no Postgres): corpus_search and the LLM seam
(dispatcher.router.invoke → (DirectionAdjudication, run_id), dispatcher.curator.build → prompt)
are patched, and the state methods are AsyncMocks on a make_state() dispatcher.

Covers:
  - _verdict: pass iff independent floors met AND novel AND impactful AND not redundant.
  - _prior_outcome: ANSWERED only for concluded / decisive-finding directions; a dead attempt
    without one reads ATTEMPTED BUT NOT ANSWERED (the 2026-06-12 all-held livelock fix).
  - _build_adjudicate: outcome tags + lab-authored prior-art annotation reach the prompt.
  - handle_direction_adjudicate: happy path (adjudicate each direction, derive verdict, persist with
    nearest prior art); the redundant/low-score → hold; the nothing-to-adjudicate short-circuit;
    a router error leaves a direction un-adjudicated (no persist) so it retries; reconsider_held
    re-adjudicates held directions (the UPSERT can flip a hold to a pass).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import agents.novelty.handler as NH
from agents.novelty.handler import _build_adjudicate, _prior_outcome, _verdict, handle_direction_adjudicate
from agents.novelty.schemas import DirectionAdjudication
from tests._helpers import make_dispatcher, make_state

pytestmark = pytest.mark.asyncio


def _event(event_id=1, **payload):
    return {"id": event_id, "payload": payload}


def _prior(statement="old direction about RAG", status="invalidated", supported=None, confidence=None):
    return {
        "id": 50,
        "statement": statement,
        "status": status,
        "finding_supported": supported,
        "finding_confidence": confidence,
    }


def _adj(**over):
    base = dict(
        novelty_independent=4,
        impact_independent=4,
        is_novel=True,
        is_impactful=True,
        redundant=False,
        redundant_note="",
        rationale="closest work is X; this extends it; a practitioner picks A over B.",
    )
    base.update(over)
    return DirectionAdjudication(**base)


class _Chunk:
    def __init__(self, title, source_kind="paper"):
        self.title = title
        self.source_kind = source_kind
        self.document_id = 1
        self.trust_tier = "certified"


def _disp(monkeypatch, *, adj=None, run_id=7, directions=None, prior=None, held=None, chunks=None):
    monkeypatch.setattr(
        NH, "corpus_search", AsyncMock(return_value=[_Chunk(t) for t in (chunks or ["A Survey of GPs", "XGBoost Paper"])])
    )
    state = make_state(
        get_unadjudicated_directions=directions if directions is not None else [{"id": 61, "statement": "GP vs XGBoost"}],
        get_prior_directions_with_outcomes=prior if prior is not None else [_prior()],
        get_held_directions=held if held is not None else [],
    )
    disp = make_dispatcher(state)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="PROMPT")
    disp.router = AsyncMock()
    disp.router.invoke = AsyncMock(return_value=(adj if adj is not None else _adj(), run_id))
    disp.session = object()
    return disp


# ── _verdict ─────────────────────────────────────────────────────────────────
async def test_verdict_pass_and_hold_paths():
    assert _verdict(_adj()) == "pass"
    assert _verdict(_adj(redundant=True)) == "hold"  # a rut never passes
    assert _verdict(_adj(is_novel=False)) == "hold"
    assert _verdict(_adj(is_impactful=False)) == "hold"
    assert _verdict(_adj(novelty_independent=2)) == "hold"  # below the independent floor
    assert _verdict(_adj(impact_independent=1)) == "hold"


# ── _prior_outcome ───────────────────────────────────────────────────────────
async def test_prior_outcome_concluded_is_answered():
    assert _prior_outcome(_prior(status="concluded", supported="supported", confidence=0.8)).startswith("ANSWERED")


async def test_prior_outcome_decisive_finding_is_answered_even_if_invalidated():
    # a decisive refutation answers the question regardless of later claim status
    out = _prior_outcome(_prior(status="invalidated", supported="refuted", confidence=0.7))
    assert out.startswith("ANSWERED")
    assert "refuted" in out


async def test_prior_outcome_invalidated_without_decisive_finding_is_open_business():
    out = _prior_outcome(_prior(status="invalidated"))
    assert out.startswith("ATTEMPTED BUT NOT ANSWERED")
    assert "invalidated" in out


async def test_prior_outcome_inconclusive_finding_does_not_answer():
    out = _prior_outcome(_prior(status="retired", supported="inconclusive", confidence=0.9))
    assert out.startswith("ATTEMPTED BUT NOT ANSWERED")


async def test_prior_outcome_active_direction_is_open():
    assert _prior_outcome(_prior(status="proposed")).startswith("OPEN")


# ── _build_adjudicate (prompt content) ───────────────────────────────────────
async def test_build_adjudicate_shows_outcomes_and_lab_tags():
    layer = await _build_adjudicate(
        {
            "direction_statement": "Self-consistency on small LLMs",
            "prior_art": [
                {"title": "External Survey", "lab": False},
                {"title": "Lab Finding: MC inference", "lab": True},
            ],
            "prior_directions": [
                _prior(statement="MC inference-time techniques", status="invalidated"),
                _prior(statement="GP calibration", status="concluded", supported="supported", confidence=0.9),
            ],
        },
        None,
        None,
    )
    assert "[ATTEMPTED BUT NOT ANSWERED — invalidated, no decisive finding] MC inference-time techniques" in layer.content
    assert "[ANSWERED (finding 'supported' at confidence 0.90)] GP calibration" in layer.content
    assert "Lab Finding: MC inference ← the lab's OWN output" in layer.content
    assert "External Survey ←" not in layer.content  # external work is untagged
    assert "unfinished business" in layer.content  # the re-ask rule reached the prompt


# ── handle_direction_adjudicate ──────────────────────────────────────────────
async def test_adjudicate_happy_path_persists_pass(monkeypatch):
    disp = _disp(monkeypatch)
    out = await handle_direction_adjudicate(_event(9), disp)
    assert out == {"adjudicated": 1, "passed": 1, "held": 0}

    _, ckw = disp.curator.build.await_args
    assert ckw["invocation_type"] == "novelty.adjudicate"
    assert ckw["context"]["direction_statement"] == "GP vs XGBoost"
    assert ckw["context"]["prior_art"] == [
        {"title": "A Survey of GPs", "lab": False},
        {"title": "XGBoost Paper", "lab": False},
    ]
    assert ckw["context"]["prior_directions"] == [_prior()]

    _, pkw = disp.state.persist_direction_adjudication.await_args
    assert pkw["claim_id"] == 61
    assert pkw["verdict"] == "pass"
    assert pkw["nearest_prior_art"] == ["A Survey of GPs", "XGBoost Paper"]  # titles only persisted
    assert pkw["novelty_independent"] == 4

    _, rkw = disp.router.invoke.await_args
    assert rkw["output_schema_class"] is DirectionAdjudication
    assert rkw["step_name"] == "novelty.adjudicate"


async def test_adjudicate_redundant_direction_holds(monkeypatch):
    disp = _disp(monkeypatch, adj=_adj(redundant=True, redundant_note="repeats the RAG direction"))
    out = await handle_direction_adjudicate(_event(1), disp)
    assert out == {"adjudicated": 1, "passed": 0, "held": 1}
    _, pkw = disp.state.persist_direction_adjudication.await_args
    assert pkw["verdict"] == "hold"
    assert pkw["redundant"] is True


async def test_adjudicate_nothing_to_do(monkeypatch):
    disp = _disp(monkeypatch, directions=[])
    out = await handle_direction_adjudicate(_event(1), disp)
    assert out["adjudicated"] == 0
    disp.state.persist_direction_adjudication.assert_not_awaited()


async def test_adjudicate_router_error_leaves_unadjudicated(monkeypatch):
    disp = _disp(monkeypatch)
    disp.router.invoke = AsyncMock(side_effect=RuntimeError("model down"))
    out = await handle_direction_adjudicate(_event(1), disp)
    assert out == {"adjudicated": 0, "passed": 0, "held": 0}
    disp.state.persist_direction_adjudication.assert_not_awaited()  # retried next tick


async def test_adjudicate_two_directions_mixed(monkeypatch):
    disp = _disp(
        monkeypatch,
        directions=[{"id": 61, "statement": "novel GP work"}, {"id": 62, "statement": "rehash"}],
    )
    # first call passes, second is redundant
    disp.router.invoke = AsyncMock(side_effect=[(_adj(), 7), (_adj(redundant=True), 8)])
    out = await handle_direction_adjudicate(_event(1), disp)
    assert out == {"adjudicated": 2, "passed": 1, "held": 1}
    assert disp.state.persist_direction_adjudication.await_count == 2


# ── reconsider_held (the pacemaker's daily all-held re-look) ─────────────────
async def test_reconsider_held_readjudicates_held_directions(monkeypatch):
    disp = _disp(
        monkeypatch,
        directions=[],  # nothing fresh — only the held backlog
        held=[{"id": 90, "statement": "XGBoost vs small nets"}, {"id": 92, "statement": "Self-consistency on GSM8K"}],
    )
    out = await handle_direction_adjudicate(_event(1, reconsider_held=True), disp)
    assert out == {"adjudicated": 2, "passed": 2, "held": 0, "reconsidered": 2}
    assert disp.state.persist_direction_adjudication.await_count == 2  # UPSERT flips hold → pass
    persisted_ids = {kw["claim_id"] for _, kw in disp.state.persist_direction_adjudication.await_args_list}
    assert persisted_ids == {90, 92}


async def test_reconsider_held_merges_without_duplicating_fresh(monkeypatch):
    disp = _disp(
        monkeypatch,
        directions=[{"id": 90, "statement": "fresh AND held — counted once"}],
        held=[{"id": 90, "statement": "fresh AND held — counted once"}, {"id": 91, "statement": "FITC calibration"}],
    )
    out = await handle_direction_adjudicate(_event(1, reconsider_held=True), disp)
    assert out["adjudicated"] == 2  # 90 once + 91
    assert out["reconsidered"] == 1  # only 91 came from the held backlog


async def test_without_reconsider_flag_held_directions_stay_parked(monkeypatch):
    disp = _disp(monkeypatch, directions=[], held=[{"id": 90, "statement": "held"}])
    out = await handle_direction_adjudicate(_event(1), disp)
    assert out["adjudicated"] == 0
    disp.state.get_held_directions.assert_not_awaited()
