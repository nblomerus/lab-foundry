"""Unit tests for the experiments agent's handlers — the lab's "hands". Everything external is
mocked (no Docker, no nvidia-smi, no Postgres, no network, no LLM):

  - handle_experiment_requested : design a script via the LLM seam and queue it for the
    Quartermaster. Covers the happy path (queue_experiment kwargs + provenance), the missing-task_id
    skip, and the get_claim ValueError fallback.
  - handle_experiment_completed : interpret the numbers, nudge the direction's confidence, and
    ingest a first-party lab note. Covers the happy path (interpretation + confidence move + corpus
    event), the missing/empty-result skip, the zero-delta no-move branch, and the get_claim
    ValueError guard.
  - handle_experiment_failed    : record the failure as a researcher note. Covers the happy path
    (note mentions the failure) and the missing experiment_id skip.

The two LLM steps are `dispatcher.router.invoke(prompt, output_schema_class, ...)` → (obj, run_id)
and `dispatcher.curator.build(invocation_type, context)` → prompt; both are AsyncMocks here. The
state methods the handlers await are AsyncMocks on a make_state() dispatcher.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.experiments.handler as EH
from agents.experiments.schemas import ExperimentDesign, ExperimentReport
from tests._helpers import make_dispatcher, make_state

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _mock_image_digest(monkeypatch):
    # provenance capture shells `docker image inspect`; keep the unit tests Docker-free.
    monkeypatch.setattr(EH.sandbox, "image_digest", AsyncMock(return_value="sha256:deadbeef"))


# ── builders ───────────────────────────────────────────────────────────────────
def _event(event_id=1, **payload):
    return {"id": event_id, "payload": payload}


def _claim(statement="kernels beat MLPs on tabular data", confidence=0.5):
    return SimpleNamespace(statement=statement, confidence=confidence)


def _design(**over):
    base = dict(
        hypothesis="a kernel SVM beats a small MLP on make_classification",
        code="import json\nprint(json.dumps({'acc': 0.9}))\n",
        requires_gpu=False,
        gpu_mem_mb=None,
        est_wall_clock_s=300,
        est_mem_mb=1024,
        seed=7,
        dataset_plan="synthesize make_classification",
    )
    base.update(over)
    return ExperimentDesign(**base)


def _report(**over):
    base = dict(
        summary="kernel SVM hit 0.90 vs 0.84 for the MLP, a +0.06 accuracy delta.",
        confidence=0.4,
        narrative_note="I ran a kernel SVM against a small MLP and the SVM won by 6 points.",
        supports_direction=True,
        confidence_delta=0.1,
    )
    base.update(over)
    return ExperimentReport(**base)


def _disp(*, design=None, report=None, run_id=7, **state_returns):
    """A dispatcher with AsyncMock curator/router + a make_state() state preset with returns."""
    # Default the synthesis trigger's experiment count to 0 so completed-handler tests that
    # don't care about synthesis don't fire it (the handler compares this int to a threshold).
    state_returns.setdefault("count_completed_experiments_for_claim", 0)
    # The design path reads prior experiments to vary the series — empty by default.
    state_returns.setdefault("get_completed_experiments_for_claim", [])
    state = make_state(**state_returns)
    disp = make_dispatcher(state)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="PROMPT")
    disp.router = AsyncMock()
    obj = design if design is not None else report
    disp.router.invoke = AsyncMock(return_value=(obj, run_id))
    disp.session = object()
    return disp


# ══════════════════════════════════════════════════════════════════════════════
# handle_experiment_requested
# ══════════════════════════════════════════════════════════════════════════════
async def test_requested_happy_path_queues_experiment():
    design = _design()
    disp = _disp(design=design, run_id=11, get_claim=_claim(), queue_experiment=42)
    out = await EH.handle_experiment_requested(
        _event(5, claim_id=3, task_id=99, hypothesis="kernels win", goal="settle the number"),
        disp,
    )

    assert out["queued_experiment"] == 42
    assert out["claim_id"] == 3
    assert out["design_run_id"] == 11

    disp.state.get_claim.assert_awaited_once_with(3)
    disp.state.queue_experiment.assert_awaited_once()
    _, kw = disp.state.queue_experiment.await_args
    assert kw["task_id"] == 99
    assert kw["kind"] == "code"
    assert kw["code"] == design.code
    assert kw["params"]["hypothesis"] == design.hypothesis
    assert kw["params"]["claim_id"] == 3
    prov = kw["provenance"]
    assert prov["image"] == "labfoundry-experiment:py311"
    assert prov["seed"] == design.seed
    assert prov["code_hash"] and isinstance(prov["code_hash"], str)


async def test_requested_curator_and_router_invoked_correctly():
    design = _design()
    disp = _disp(design=design, get_claim=_claim(statement="my direction"), queue_experiment=1)
    await EH.handle_experiment_requested(_event(8, claim_id=2, task_id=1, hypothesis="h", goal="g"), disp)

    _, ckw = disp.curator.build.await_args
    assert ckw["invocation_type"] == "experiments.design"
    assert ckw["context"]["hypothesis"] == "h"
    assert ckw["context"]["goal"] == "g"
    assert ckw["context"]["claim_statement"] == "my direction"

    _, rkw = disp.router.invoke.await_args
    assert rkw["prompt"] == "PROMPT"
    assert rkw["output_schema_class"] is ExperimentDesign
    assert rkw["triggered_by_event_id"] == 8
    assert rkw["step_name"] == "experiments.design"
    assert rkw["session"] is disp.session


async def test_requested_passes_prior_hypotheses_to_vary_the_series():
    # A driven series: prior experiments' hypotheses flow into the design context so the next one
    # tests a DISTINCT facet instead of repeating.
    disp = _disp(
        design=_design(),
        get_claim=_claim(statement="GP vs XGBoost"),
        queue_experiment=1,
        get_completed_experiments_for_claim=[
            {"params": {"hypothesis": "8-bit GP within 2% RMSE of XGBoost"}},
            {"params": {"hypothesis": "GP calibration ECE beats XGBoost"}},
            {"params": {}},  # no hypothesis → dropped
        ],
    )
    await EH.handle_experiment_requested(_event(3, claim_id=7, task_id=1), disp)
    _, ckw = disp.curator.build.await_args
    assert ckw["context"]["prior_hypotheses"] == [
        "8-bit GP within 2% RMSE of XGBoost",
        "GP calibration ECE beats XGBoost",
    ]


async def test_requested_wall_clock_capped_at_1800():
    design = _design(est_wall_clock_s=1800)  # schema caps at 1800; handler also min(1800, ...)
    disp = _disp(design=design, get_claim=_claim(), queue_experiment=1)
    await EH.handle_experiment_requested(_event(1, claim_id=1, task_id=1), disp)
    _, kw = disp.state.queue_experiment.await_args
    assert kw["wall_clock_budget_s"] == 1800


async def test_requested_gpu_default_mem_when_requires_gpu():
    design = _design(requires_gpu=True, gpu_mem_mb=None)
    disp = _disp(design=design, get_claim=_claim(), queue_experiment=1)
    await EH.handle_experiment_requested(_event(1, claim_id=1, task_id=1), disp)
    _, kw = disp.state.queue_experiment.await_args
    assert kw["requires_gpu"] is True
    assert kw["gpu_mem_mb"] == 4096  # falls back to 4096 when GPU needed but unspecified


async def test_requested_missing_task_id_skips():
    disp = _disp(design=_design(), queue_experiment=42)
    out = await EH.handle_experiment_requested(_event(1, claim_id=3), disp)  # no task_id
    assert out["skipped"] is True
    assert "task_id" in out["reason"]
    disp.state.queue_experiment.assert_not_awaited()
    disp.router.invoke.assert_not_awaited()


async def test_requested_claim_not_found_falls_back_to_empty_statement():
    disp = _disp(design=_design(), queue_experiment=42)
    disp.state.get_claim = AsyncMock(side_effect=ValueError("no such claim"))
    out = await EH.handle_experiment_requested(_event(1, claim_id=404, task_id=7), disp)
    assert out["queued_experiment"] == 42
    _, ckw = disp.curator.build.await_args
    assert ckw["context"]["claim_statement"] == ""  # fell back to empty on ValueError


async def test_requested_no_claim_id_skips_lookup():
    disp = _disp(design=_design(), queue_experiment=42)
    out = await EH.handle_experiment_requested(_event(1, task_id=7), disp)  # claim_id absent
    assert out["queued_experiment"] == 42
    assert out["claim_id"] is None
    disp.state.get_claim.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# handle_experiment_completed
# ══════════════════════════════════════════════════════════════════════════════
def _exp(**over):
    base = dict(
        kind="code",
        result={"acc": 0.9, "delta": 0.06},
        params={"hypothesis": "kernels beat MLPs"},
        provenance={"seed": 7, "image": "labfoundry-experiment:py311", "code_hash": "abc123"},
    )
    base.update(over)
    return base


async def test_completed_happy_path_interprets_moves_conf_ingests():
    report = _report(confidence_delta=0.1)
    disp = _disp(report=report, run_id=9, get_experiment=_exp(), get_claim=_claim(confidence=0.5))
    out = await EH.handle_experiment_completed(_event(2, experiment_id=42, claim_id=3, task_id=1), disp)

    # interpretation persisted with the narrative note
    disp.state.set_experiment_interpretation.assert_awaited_once()
    args, _ = disp.state.set_experiment_interpretation.await_args
    assert args[0] == 42
    assert args[1] == report.summary
    assert args[2] == 9  # run_id
    assert args[3] == report.narrative_note

    # confidence nudged ~0.5 + 0.1 = 0.6
    disp.state.update_claim_confidence.assert_awaited_once()
    cargs, ckw = disp.state.update_claim_confidence.await_args
    assert cargs[0] == 3
    assert abs(cargs[1] - 0.6) < 1e-9
    assert ckw["run_id"] == 9
    assert out["confidence"] == [0.5, 0.6]
    assert out["supports_direction"] is True
    assert out["ingested_note"] is True

    # first-party lab note AND dataset card ingested into the corpus (two emits)
    assert disp.state.emit_corpus_event.await_count == 2
    kinds = {c.kwargs["payload"]["source"]["source_kind"] for c in disp.state.emit_corpus_event.await_args_list}
    assert kinds == {"lab_experiment", "lab_dataset"}
    for c in disp.state.emit_corpus_event.await_args_list:
        assert c.args[0] == "source.discovered"
        payload = c.kwargs["payload"]
        assert isinstance(payload["content"], str) and payload["content"]
        assert "provenance" in payload
    assert out["ingested_dataset"] is True
    disp.state.set_experiment_dataset_refs.assert_awaited_once()


async def test_completed_curator_and_router_invoked_correctly():
    disp = _disp(report=_report(), get_experiment=_exp(), get_claim=_claim())
    await EH.handle_experiment_completed(_event(3, experiment_id=7, claim_id=2), disp)
    _, ckw = disp.curator.build.await_args
    assert ckw["invocation_type"] == "experiments.interpret"
    assert ckw["context"]["result"] == {"acc": 0.9, "delta": 0.06}
    assert ckw["context"]["hypothesis"] == "kernels beat MLPs"
    _, rkw = disp.router.invoke.await_args
    assert rkw["output_schema_class"] is ExperimentReport
    assert rkw["step_name"] == "experiments.interpret"


async def test_completed_no_experiment_skips():
    disp = _disp(report=_report(), get_experiment=None)
    out = await EH.handle_experiment_completed(_event(1, experiment_id=42, claim_id=3), disp)
    assert out["skipped"] is True
    disp.state.set_experiment_interpretation.assert_not_awaited()
    disp.router.invoke.assert_not_awaited()


async def test_completed_missing_experiment_id_skips():
    disp = _disp(report=_report(), get_experiment=_exp())
    out = await EH.handle_experiment_completed(_event(1, claim_id=3), disp)  # no experiment_id
    assert out["skipped"] is True
    disp.state.get_experiment.assert_not_awaited()


async def test_completed_empty_result_skips():
    disp = _disp(report=_report(), get_experiment=_exp(result=None))
    out = await EH.handle_experiment_completed(_event(1, experiment_id=42, claim_id=3), disp)
    assert out["skipped"] is True
    disp.router.invoke.assert_not_awaited()


async def test_completed_zero_delta_does_not_move_confidence():
    disp = _disp(report=_report(confidence_delta=0.0), get_experiment=_exp(), get_claim=_claim())
    out = await EH.handle_experiment_completed(_event(1, experiment_id=42, claim_id=3), disp)
    disp.state.update_claim_confidence.assert_not_awaited()  # zero delta → no move
    assert out["confidence"] is None
    assert disp.state.emit_corpus_event.await_count == 2  # note + dataset ingested


async def test_completed_no_claim_id_skips_confidence():
    disp = _disp(report=_report(confidence_delta=0.2), get_experiment=_exp())
    out = await EH.handle_experiment_completed(_event(1, experiment_id=42), disp)  # claim_id absent
    disp.state.update_claim_confidence.assert_not_awaited()
    assert out["claim_id"] is None
    assert disp.state.emit_corpus_event.await_count == 2  # note + dataset


async def test_completed_claim_not_found_skips_confidence_but_ingests():
    disp = _disp(report=_report(confidence_delta=0.2), get_experiment=_exp())
    # get_claim raises ValueError when confidence move is attempted (claim inactive / missing)
    disp.state.get_claim = AsyncMock(side_effect=ValueError("claim not active"))
    out = await EH.handle_experiment_completed(_event(1, experiment_id=42, claim_id=3), disp)
    disp.state.update_claim_confidence.assert_not_awaited()
    assert out["confidence"] is None
    assert disp.state.emit_corpus_event.await_count == 2  # experiment stands -> note + dataset


async def test_completed_confidence_clamped_to_one():
    # 0.95 + 0.3 would be 1.25 → clamped to 1.0
    disp = _disp(report=_report(confidence_delta=0.3), get_experiment=_exp(), get_claim=_claim(confidence=0.95))
    await EH.handle_experiment_completed(_event(1, experiment_id=42, claim_id=3), disp)
    cargs, _ = disp.state.update_claim_confidence.await_args
    assert cargs[1] == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# handle_experiment_failed
# ══════════════════════════════════════════════════════════════════════════════
async def test_failed_records_failure_note():
    disp = _disp(get_experiment={"error": "boom"})
    out = await EH.handle_experiment_failed(_event(1, experiment_id=42, claim_id=3, task_id=1), disp)
    assert out["failed"] is True
    assert out["experiment_id"] == 42
    assert out["handled"] is True

    disp.state.set_experiment_interpretation.assert_awaited_once()
    args, _ = disp.state.set_experiment_interpretation.await_args
    assert args[0] == 42
    assert args[1] is None  # no summary for a failure
    assert args[2] is None  # no run_id
    assert "boom" in args[3]  # the note mentions the failure
    assert "failed" in args[3].lower()


async def test_failed_no_error_recorded_uses_placeholder():
    disp = _disp(get_experiment={})  # no "error" key
    out = await EH.handle_experiment_failed(_event(1, experiment_id=7), disp)
    assert out["failed"] is True
    args, _ = disp.state.set_experiment_interpretation.await_args
    assert "no error recorded" in args[3]


async def test_failed_experiment_none_uses_placeholder():
    disp = _disp(get_experiment=None)
    out = await EH.handle_experiment_failed(_event(1, experiment_id=7), disp)
    assert out["failed"] is True
    args, _ = disp.state.set_experiment_interpretation.await_args
    assert "no error recorded" in args[3]


async def test_failed_missing_experiment_id_skips():
    disp = _disp(get_experiment={"error": "boom"})
    out = await EH.handle_experiment_failed(_event(1, claim_id=3), disp)  # no experiment_id
    assert out["skipped"] is True
    disp.state.get_experiment.assert_not_awaited()
    disp.state.set_experiment_interpretation.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# curator task_data builders (the recipe callbacks registered at import)
# ══════════════════════════════════════════════════════════════════════════════
async def test_build_design_includes_ctx_fields():
    ctx = {
        "hypothesis": "kernels win",
        "goal": "settle the number",
        "claim_statement": "kernels beat MLPs",
        "lab_constraints": "single modest GPU",
    }
    layer = await EH._build_design(ctx, state=None, memory=None)
    assert layer.name == "task_data"
    assert layer.priority == 1
    assert "kernels win" in layer.content
    assert "settle the number" in layer.content
    assert "kernels beat MLPs" in layer.content
    assert "single modest GPU" in layer.content
    assert "ExperimentDesign" in layer.content


async def test_build_design_empty_ctx_uses_fallbacks():
    layer = await EH._build_design({}, state=None, memory=None)
    assert "(no direction statement)" in layer.content
    assert "(no explicit goal)" in layer.content
    assert EH._LAB_CONSTRAINTS in layer.content  # falls back to the lab envelope constant


async def test_build_interpret_includes_result_and_hypothesis():
    ctx = {
        "kind": "code",
        "params": {"hypothesis": "h"},
        "result": {"acc": 0.9},
        "hypothesis": "kernels win",
        "claim_statement": "kernels beat MLPs",
    }
    layer = await EH._build_interpret(ctx, state=None, memory=None)
    assert layer.name == "task_data"
    assert '"acc": 0.9' in layer.content
    assert "kernels win" in layer.content
    assert "kernels beat MLPs" in layer.content


async def test_build_interpret_empty_ctx_uses_fallbacks():
    layer = await EH._build_interpret({}, state=None, memory=None)
    assert "(no direction statement)" in layer.content
    assert "(none recorded)" in layer.content


async def test_build_debug_includes_code_and_error():
    ctx = {
        "code": "print('hi')",
        "error": "ImportError: no module named foo",
        "hypothesis": "kernels win",
        "claim_statement": "kernels beat MLPs",
        "iteration": 2,
        "lab_constraints": "single modest GPU",
    }
    layer = await EH._build_debug(ctx, state=None, memory=None)
    assert layer.name == "task_data"
    assert "attempt 2" in layer.content
    assert "print('hi')" in layer.content
    assert "ImportError: no module named foo" in layer.content
    assert "single modest GPU" in layer.content


async def test_build_debug_empty_ctx_uses_fallbacks():
    layer = await EH._build_debug({}, state=None, memory=None)
    assert "attempt 1" in layer.content
    assert "(no error captured)" in layer.content
    assert "(no direction statement)" in layer.content
    assert EH._LAB_CONSTRAINTS in layer.content


async def test_requested_infeasible_records_failed_run_and_skips_queue_path():
    """An infeasible design (needs a pretrained LLM / network) is recorded as a FAILED run with
    the reason as its note — the coverage driver counts the attempt, the re-armer sees a handled
    run, and NO sandbox code is queued. Simulating the outcome instead is forbidden upstream."""
    design = _design(code="", infeasible=True, infeasible_reason="needs a pretrained 7B model")
    disp = _disp(design=design, get_claim=_claim(), queue_experiment=51)
    out = await EH.handle_experiment_requested(_event(5, claim_id=3, task_id=99), disp)
    assert out["infeasible"] is True and out["experiment_id"] == 51
    assert "pretrained 7B model" in out["reason"]
    _, kw = disp.state.queue_experiment.await_args
    assert kw["code"] == "" and kw["params"]["infeasible"] is True
    disp.state.record_experiment_result.assert_awaited_once()
    _args, rkw = disp.state.record_experiment_result.await_args
    assert rkw["status"] == "failed" and "infeasible on lab sandbox" in rkw["error"]
    disp.state.set_experiment_interpretation.assert_awaited_once()
    note = disp.state.set_experiment_interpretation.await_args.args[3]
    assert "Untestable on the lab's offline sandbox" in note


async def test_requested_floors_tiny_wall_clock_budget():
    """Design-estimated 10-60s budgets killed runs before one attempt finished — floor at 120s."""
    design = _design(est_wall_clock_s=10)
    disp = _disp(design=design, get_claim=_claim(), queue_experiment=1)
    await EH.handle_experiment_requested(_event(5, claim_id=3, task_id=99), disp)
    _, kw = disp.state.queue_experiment.await_args
    assert kw["wall_clock_budget_s"] == 120
