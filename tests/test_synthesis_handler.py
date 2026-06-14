"""Unit tests for the synthesis agent — the lab's terminal step. Everything external is
mocked (no Postgres, no LLM): the LLM seam is `dispatcher.router.invoke(...) → (ResearchFinding,
run_id)` with `dispatcher.curator.build(...) → prompt`, and the state methods are AsyncMocks on a
make_state() dispatcher.

Covers:
  - handle_finding_synthesize: happy path (compose → persist_research_finding + ingest a lab_finding
    doc + graduation), the too-few-experiments skip, the get_claim ValueError skip, the
    already-synthesized idempotency skip, the curator/router wiring, and grounded_in filtering.
  - the condition-driven trigger inside handle_experiment_completed (emits finding.synthesize once
    a direction crosses SYNTHESIS_MIN_EXPERIMENTS completed runs).
  - _graduate_to mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.experiments.handler as EH  # for EH.sandbox.image_digest
import agents.synthesis.handler as SH
from agents.researcher import experiment_interpret as EI
from agents.synthesis.handler import _graduate_to, handle_finding_synthesize
from agents.synthesis.schemas import ResearchFinding
from tests._helpers import make_dispatcher, make_state

pytestmark = pytest.mark.asyncio


# ── builders ───────────────────────────────────────────────────────────────────
def _event(event_id=1, **payload):
    return {"id": event_id, "payload": payload}


def _exp(experiment_id, hypothesis="A beats B", **result):
    return {
        "experiment_id": experiment_id,
        "kind": "code",
        "params": {"hypothesis": hypothesis},
        "result": result or {"acc": 0.9},
        "interpretation": "clean signal",
        "researcher_notes": "ran it, A won",
        "completed_at": None,
    }


def _finding(**over):
    base = dict(
        headline="On tabular regression, quantized GPs match XGBoost RMSE at 8-bit.",
        claim="8-bit quantized GPs stay within 2% RMSE of XGBoost on 10 UCI datasets.",
        supported="supported",
        method="10 UCI datasets, GPyTorch 8-bit vs XGBoost, RMSE + calibration.",
        key_numbers="mean RMSE delta 1.4% (CI 0.8-2.1), 3.2x lower memory.",
        limitations="UCI toy scale, single GPU, no large-dataset regime.",
        so_what="A fintech data scientist can ship a calibrated GP instead of XGBoost.",
        next_step="repeat on a 1M-row dataset.",
        confidence=0.7,
        grounded_in_experiments=[10, 11, 12],
    )
    base.update(over)
    return ResearchFinding(**base)


def _disp(*, finding=None, run_id=7, **state_returns):
    state = make_state(**state_returns)
    disp = make_dispatcher(state)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="PROMPT")
    disp.router = AsyncMock()
    disp.router.invoke = AsyncMock(return_value=(finding if finding is not None else _finding(), run_id))
    disp.session = object()
    return disp


def _happy_state(**over):
    base = dict(
        get_claim=SimpleNamespace(statement="Quantized GPs vs XGBoost on tabular", confidence=0.5),
        get_completed_experiments_for_claim=[_exp(10), _exp(11), _exp(12)],
        latest_finding_n_for_claim=None,
        get_claim_goals_text="- expect: RMSE within 2% · kill if: > 5% worse",
        persist_research_finding={"finding_id": 5, "finding_claim_id": 100, "graduated_to": "concluded"},
    )
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# handle_finding_synthesize
# ══════════════════════════════════════════════════════════════════════════════
async def test_synthesize_happy_path_persists_and_ingests():
    disp = _disp(**_happy_state())
    out = await handle_finding_synthesize(_event(9, claim_id=61), disp)

    assert out["claim_id"] == 61
    assert out["finding_id"] == 5
    assert out["supported"] == "supported"
    assert out["n_experiments"] == 3
    assert out["graduated_to"] == "concluded"

    # persist_research_finding got the finding + graduation
    _, pkw = disp.state.persist_research_finding.await_args
    assert pkw["direction_claim_id"] == 61
    assert pkw["supported"] == "supported"
    assert pkw["n_experiments"] == 3
    assert pkw["graduate_to"] == "concluded"  # supported @0.7 (decisive + confident) → concluded
    assert pkw["grounded_in"] == ["exp:10", "exp:11", "exp:12"]

    # ingested as a first-party lab_finding doc
    args, ekw = disp.state.emit_corpus_event.await_args
    assert args[0] == "source.discovered"
    src = ekw["payload"]["source"]
    assert src["source_kind"] == "lab_finding"
    assert src["canonical_key"] == "finding:61:3"
    assert ekw["payload"]["provenance"]["grounded_in_experiments"] == ["exp:10", "exp:11", "exp:12"]
    assert ekw["dedup_key"] == "finding-doc-61-3"


async def test_synthesize_curator_and_router_wired():
    disp = _disp(**_happy_state())
    await handle_finding_synthesize(_event(8, claim_id=61), disp)

    _, ckw = disp.curator.build.await_args
    assert ckw["invocation_type"] == "synthesis.compose"
    assert ckw["context"]["direction_statement"] == "Quantized GPs vs XGBoost on tabular"
    assert len(ckw["context"]["experiments"]) == 3

    _, rkw = disp.router.invoke.await_args
    assert rkw["output_schema_class"] is ResearchFinding
    assert rkw["triggered_by_event_id"] == 8
    assert rkw["step_name"] == "synthesis.compose"


async def test_synthesize_filters_grounded_in_to_real_experiments():
    # The LLM cites experiment 999 which isn't in the completed set → dropped.
    disp = _disp(finding=_finding(grounded_in_experiments=[10, 999]), **_happy_state())
    await handle_finding_synthesize(_event(1, claim_id=61), disp)
    _, pkw = disp.state.persist_research_finding.await_args
    assert pkw["grounded_in"] == ["exp:10"]


async def test_synthesize_skips_when_too_few_experiments():
    disp = _disp(**_happy_state(get_completed_experiments_for_claim=[_exp(10), _exp(11)]))
    out = await handle_finding_synthesize(_event(1, claim_id=61), disp)
    assert out["skipped"] is True
    disp.state.persist_research_finding.assert_not_awaited()


async def test_synthesize_skips_on_missing_claim():
    state_returns = _happy_state()
    disp = _disp(**state_returns)
    disp.state.get_claim.side_effect = ValueError("not active")
    out = await handle_finding_synthesize(_event(1, claim_id=61), disp)
    assert out["skipped"] is True
    disp.state.persist_research_finding.assert_not_awaited()


async def test_synthesize_idempotent_when_already_synthesized():
    # A finding already rests on >= the current experiment count → skip.
    disp = _disp(**_happy_state(latest_finding_n_for_claim=3))
    out = await handle_finding_synthesize(_event(1, claim_id=61), disp)
    assert out["skipped"] is True
    disp.state.persist_research_finding.assert_not_awaited()


async def test_synthesize_no_claim_id():
    disp = _disp(**_happy_state())
    out = await handle_finding_synthesize(_event(1), disp)
    assert out["skipped"] is True


# ══════════════════════════════════════════════════════════════════════════════
# the condition-driven trigger inside handle_experiment_completed
# ══════════════════════════════════════════════════════════════════════════════
async def test_experiment_completed_triggers_synthesis_when_enough(monkeypatch):
    monkeypatch.setattr(EH.sandbox, "image_digest", AsyncMock(return_value="sha256:x"))
    report = SimpleNamespace(
        summary="s", confidence=0.4, narrative_note="n", supports_direction=True, confidence_delta=0.1
    )
    state = make_state(
        get_experiment={"result": {"acc": 0.9}, "params": {"hypothesis": "h"}, "provenance": {}},
        get_claim=SimpleNamespace(statement="d", confidence=0.5),
        count_completed_experiments_for_claim=SH.SYNTHESIS_MIN_EXPERIMENTS,
    )
    disp = make_dispatcher(state)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="P")
    disp.router = AsyncMock()
    disp.router.invoke = AsyncMock(return_value=(report, 7))
    disp.session = object()

    out = await EI.handle_experiment_completed(_event(1, experiment_id=50, claim_id=61), disp)
    assert out["synthesis_triggered"] is True
    # one of the emit_corpus_event calls is the finding.synthesize trigger
    types = [(c.args[0] if c.args else c.kwargs.get("event_type")) for c in disp.state.emit_corpus_event.await_args_list]
    assert "finding.synthesize" in types


async def test_experiment_completed_no_trigger_below_threshold(monkeypatch):
    monkeypatch.setattr(EH.sandbox, "image_digest", AsyncMock(return_value="sha256:x"))
    report = SimpleNamespace(
        summary="s", confidence=0.4, narrative_note="n", supports_direction=True, confidence_delta=0.1
    )
    state = make_state(
        get_experiment={"result": {"acc": 0.9}, "params": {"hypothesis": "h"}, "provenance": {}},
        get_claim=SimpleNamespace(statement="d", confidence=0.5),
        count_completed_experiments_for_claim=SH.SYNTHESIS_MIN_EXPERIMENTS - 1,
    )
    disp = make_dispatcher(state)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="P")
    disp.router = AsyncMock()
    disp.router.invoke = AsyncMock(return_value=(report, 7))
    disp.session = object()

    out = await EI.handle_experiment_completed(_event(1, experiment_id=50, claim_id=61), disp)
    assert out["synthesis_triggered"] is False
    types = [c.args[0] for c in disp.state.emit_corpus_event.await_args_list]
    assert "finding.synthesize" not in types


# ══════════════════════════════════════════════════════════════════════════════
# _graduate_to mapping
# ══════════════════════════════════════════════════════════════════════════════
async def test_graduate_to_mapping():
    # Confident + decisive (not inconclusive) → CONCLUDED (terminal result).
    assert _graduate_to("supported", 0.8) == "concluded"
    assert _graduate_to("supported", 0.6) == "concluded"
    assert _graduate_to("refuted", 0.95) == "concluded"  # a definitive negative result still concludes
    assert _graduate_to("mixed", 0.9) == "concluded"
    # Below the conclude bar → stays OPEN (more experiments might settle it).
    assert _graduate_to("supported", 0.59) == "weakly_supported"
    assert _graduate_to("supported", 0.4) == "weakly_supported"
    assert _graduate_to("refuted", 0.5) == "tested"
    # Inconclusive NEVER concludes, regardless of confidence.
    assert _graduate_to("inconclusive", 0.9) == "tested"


# ── data-realism: discount synthetic-only findings + the env-gated hard conclude gate ──
async def test_worst_realism_and_has_real():
    exps = [{"data_realism": "real"}, {"data_realism": "synthetic"}, {"data_realism": None}]
    assert SH._worst_realism(exps) == "synthetic"  # weakest wins; None → synthetic
    assert SH._has_real(exps) is True
    assert SH._worst_realism([{"data_realism": "builtin"}]) == "builtin"
    assert SH._has_real([{"data_realism": "builtin"}]) is False


async def test_graduate_to_real_data_gate(monkeypatch):
    # default (gate OFF): a decisive synthetic-only finding still concludes
    monkeypatch.setattr(SH, "SYNTHESIS_REQUIRE_REAL", False)
    assert SH._graduate_to("supported", 0.7, has_real=False) == "concluded"
    # gate ON: synthetic-only cannot conclude → parks; real evidence still concludes
    monkeypatch.setattr(SH, "SYNTHESIS_REQUIRE_REAL", True)
    assert SH._graduate_to("supported", 0.7, has_real=False) == "weakly_supported"
    assert SH._graduate_to("supported", 0.7, has_real=True) == "concluded"


async def test_compose_prompt_discounts_synthetic_only():
    ctx = {"direction_statement": "d", "experiments": [{"data_realism": "synthetic", "params": {}, "result": {}}]}
    layer = await SH._build_compose(ctx, state=None, memory=None)
    assert "DATA REALISM" in layer.content and "SYNTHETIC" in layer.content
    # a run with real evidence gets no synthetic-only warning
    ctx2 = {"direction_statement": "d", "experiments": [{"data_realism": "real", "params": {}, "result": {}}]}
    layer2 = await SH._build_compose(ctx2, state=None, memory=None)
    assert "every experiment above used SYNTHETIC" not in layer2.content
