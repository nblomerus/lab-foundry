"""Unit tests for the novelty agent — the independent adjudicator. Everything external is
mocked (no corpus search, no LLM, no Postgres): corpus_search and the LLM seam
(dispatcher.router.invoke → (DirectionAdjudication, run_id), dispatcher.curator.build → prompt)
are patched, and the state methods are AsyncMocks on a make_state() dispatcher.

Covers:
  - _verdict: pass iff independent floors met AND novel AND impactful AND not redundant.
  - handle_direction_adjudicate: happy path (adjudicate each direction, derive verdict, persist with
    nearest prior art); the redundant/low-score → hold; the nothing-to-adjudicate short-circuit;
    a router error leaves a direction un-adjudicated (no persist) so it retries.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import agents.novelty.handler as NH
from agents.novelty.handler import _verdict, handle_direction_adjudicate
from agents.novelty.schemas import DirectionAdjudication
from tests._helpers import make_dispatcher, make_state

pytestmark = pytest.mark.asyncio


def _event(event_id=1):
    return {"id": event_id, "payload": {}}


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
    def __init__(self, title):
        self.title = title
        self.document_id = 1
        self.trust_tier = "certified"


def _disp(monkeypatch, *, adj=None, run_id=7, directions=None, prior=None, chunks=None):
    monkeypatch.setattr(
        NH, "corpus_search", AsyncMock(return_value=[_Chunk(t) for t in (chunks or ["A Survey of GPs", "XGBoost Paper"])])
    )
    state = make_state(
        get_unadjudicated_directions=directions if directions is not None else [{"id": 61, "statement": "GP vs XGBoost"}],
        get_prior_direction_statements=prior if prior is not None else ["old direction about RAG"],
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


# ── handle_direction_adjudicate ──────────────────────────────────────────────
async def test_adjudicate_happy_path_persists_pass(monkeypatch):
    disp = _disp(monkeypatch)
    out = await handle_direction_adjudicate(_event(9), disp)
    assert out == {"adjudicated": 1, "passed": 1, "held": 0}

    _, ckw = disp.curator.build.await_args
    assert ckw["invocation_type"] == "novelty.adjudicate"
    assert ckw["context"]["direction_statement"] == "GP vs XGBoost"
    assert ckw["context"]["prior_art"] == ["A Survey of GPs", "XGBoost Paper"]

    _, pkw = disp.state.persist_direction_adjudication.await_args
    assert pkw["claim_id"] == 61
    assert pkw["verdict"] == "pass"
    assert pkw["nearest_prior_art"] == ["A Survey of GPs", "XGBoost Paper"]
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
