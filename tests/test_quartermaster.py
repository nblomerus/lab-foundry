"""Tests for harness/quartermaster.py — the lab's experiment resource manager.

FULLY MOCKED: no Docker, no nvidia-smi, no Postgres, no network, no LLM.

The seams we exercise:
  * ``state`` is an AsyncMock with the experiment methods the QM awaits
    (get_queued_experiments / get_running_experiments / mark_experiment_running /
    heartbeat_experiment / record_experiment_result / kill_experiment /
    emit_corpus_event) — never a real DB.
  * ``agents.experiments.sandbox`` is patched at its kill / force_remove call
    sites (AsyncMock) so no `docker` ever runs; container_name stays real.
  * ``harness.quartermaster.run_experiment`` is monkeypatched to an AsyncMock in
    the allocate tests so ``allocate`` only *schedules* a coroutine (we never run
    the real session); the run_experiment tests instead monkeypatch
    ``run_code_session`` directly to drive its three terminal branches.
  * ``best_gpu_with_headroom`` is the real pure function fed synthetic gpu dicts.

Module constants (MAX_CPU / MAX_GPU / GPU_RESERVE_MB / …) are read at call time
off the module, so we monkeypatch them to control thresholds without a real clock.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import harness.quartermaster as qm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _state() -> AsyncMock:
    """An AsyncMock state with the experiment methods the QM awaits. Sensible
    defaults; individual tests override return_values as needed."""
    st = AsyncMock()
    st.get_queued_experiments.return_value = []
    st.get_running_experiments.return_value = []
    st.mark_experiment_running.return_value = True
    return st


def _patch_sandbox(monkeypatch) -> AsyncMock:
    """Replace the destructive sandbox calls (docker kill / rm) with AsyncMocks;
    keep container_name real. Returns the sandbox module so tests can assert."""
    monkeypatch.setattr(qm.sandbox, "kill", AsyncMock())
    monkeypatch.setattr(qm.sandbox, "force_remove", AsyncMock())
    return qm.sandbox


def _exp(eid: int = 1, *, requires_gpu: bool = False, **extra) -> dict:
    exp = {"id": eid, "requires_gpu": requires_gpu, "params": {"claim_id": 7}, "task_id": 3}
    exp.update(extra)
    return exp


def _gpu(index: int = 0, free_mb: float = 10000.0) -> dict:
    return {"index": index, "mem_free_mb": free_mb}


async def _drain(running: dict) -> None:
    """Await any tasks allocate() scheduled (run_experiment is an AsyncMock, so
    each completes immediately) without leaving 'pending task' warnings."""
    tasks = [t for t, _is_gpu in running.values() if isinstance(t, asyncio.Task)]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _idle_gpu_lock(monkeypatch):
    """Default the lab-activity gate to idle. The shared GPULock is a process-wide
    singleton whose ``_last_active`` persists across tests, so without this an
    earlier test that exercised the lock would make later GPU-allocate tests read
    'lab busy' and defer — an order-dependent, flaky failure (seen in CI). Tests
    that exercise the gate itself re-patch ``shared_gpu_lock`` to a busy stub."""
    monkeypatch.setattr(qm, "shared_gpu_lock", lambda: types.SimpleNamespace(busy=lambda *a, **k: False))


# ===========================================================================
# _enabled — the QUARTERMASTER env flag
# ===========================================================================
@pytest.mark.parametrize("val", ["on", "1", "true", "YES", "On"])
def test_enabled_true_values(monkeypatch, val):
    monkeypatch.setenv("QUARTERMASTER", val)
    assert qm._enabled() is True


@pytest.mark.parametrize("val", ["off", "0", "false", "no", ""])
def test_enabled_false_values(monkeypatch, val):
    monkeypatch.setenv("QUARTERMASTER", val)
    assert qm._enabled() is False


def test_enabled_unset(monkeypatch):
    monkeypatch.delenv("QUARTERMASTER", raising=False)
    assert qm._enabled() is False


# ===========================================================================
# _claim_id — params shape tolerance
# ===========================================================================
def test_claim_id_from_params():
    assert qm._claim_id({"params": {"claim_id": 42}}) == 42


def test_claim_id_none_when_missing():
    assert qm._claim_id({"params": {}}) is None
    assert qm._claim_id({}) is None


def test_claim_id_none_when_params_not_dict():
    assert qm._claim_id({"params": ["not", "a", "dict"]}) is None


# ===========================================================================
# allocate — CPU lane
# ===========================================================================
@pytest.mark.asyncio
async def test_allocate_launches_cpu_experiment(monkeypatch):
    """A queued CPU experiment with host headroom → mark_experiment_running called,
    a task is added to `running`, and the id is returned in `launched`."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())  # never run the real session
    monkeypatch.setattr(qm, "MAX_CPU", 2)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(1)]
    res = {"cpu_percent": 10.0, "mem_percent": 20.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == [1]
    st.mark_experiment_running.assert_awaited_once_with(1, qm.sandbox.container_name(1))
    assert 1 in running
    task, is_gpu = running[1]
    assert isinstance(task, asyncio.Task) and is_gpu is False
    await _drain(running)
    qm.run_experiment.assert_awaited_once()  # the scheduled task ran the (mocked) runner


@pytest.mark.asyncio
async def test_allocate_cpu_saturated_not_launched(monkeypatch):
    """When the CPU lane is already at MAX_CPU running, a queued CPU experiment is
    not launched."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_CPU", 1)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(2)]
    res = {"cpu_percent": 5.0, "mem_percent": 5.0, "gpus": []}
    # one CPU experiment already running (is_gpu=False)
    running = {1: (object(), False)}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()
    assert 2 not in running


@pytest.mark.asyncio
async def test_allocate_cpu_over_headroom_not_launched(monkeypatch):
    """cpu_percent at/above CPU_HEADROOM_PCT → no CPU launch (protect the host)."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "CPU_HEADROOM_PCT", 80.0)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(1)]
    res = {"cpu_percent": 95.0, "mem_percent": 10.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_cpu_over_mem_headroom_not_launched(monkeypatch):
    """mem_percent at/above MEM_HEADROOM_PCT → no CPU launch."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MEM_HEADROOM_PCT", 85.0)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(1)]
    res = {"cpu_percent": 10.0, "mem_percent": 99.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_skips_experiment_already_in_running(monkeypatch):
    """A queued id that is already in the `running` dict is skipped (no double-claim)."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_CPU", 5)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(1)]
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": []}
    running = {1: (object(), False)}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_mark_running_false_gates_double_claim(monkeypatch):
    """mark_experiment_running returning False (lost the race) → not launched."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_CPU", 2)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(1)]
    st.mark_experiment_running.return_value = False  # someone else claimed it
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_awaited_once()
    assert running == {}
    qm.run_experiment.assert_not_awaited()  # nothing scheduled


# ===========================================================================
# allocate — GPU lane (serialized + VRAM-gated)
# ===========================================================================
@pytest.mark.asyncio
async def test_allocate_launches_gpu_experiment_with_headroom(monkeypatch):
    """A GPU experiment with a GPU that has VRAM headroom → launched, marked as the
    gpu lane in `running`, and the chosen device is recorded on the exp dict."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    monkeypatch.setattr(qm, "GPU_MEM_DEFAULT", 4096)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    exp = _exp(5, requires_gpu=True)
    st.get_queued_experiments.return_value = [exp]
    res = {"cpu_percent": 90.0, "mem_percent": 90.0, "gpus": [_gpu(index=3, free_mb=20000.0)]}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == [5]
    assert running[5][1] is True  # the gpu lane
    assert exp["_gpu_device"] == "3"  # best_gpu_with_headroom picked device 3
    st.mark_experiment_running.assert_awaited_once_with(5, qm.sandbox.container_name(5))
    await _drain(running)


@pytest.mark.asyncio
async def test_allocate_gpu_no_headroom_not_launched(monkeypatch):
    """No GPU leaves the reserve free after the experiment's need → not launched."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    monkeypatch.setattr(qm, "GPU_MEM_DEFAULT", 4096)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(5, requires_gpu=True)]
    # free 5000 - need 4096 = 904 < reserve 2048 → no headroom
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": [_gpu(index=0, free_mb=5000.0)]}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_gpu_no_gpus_at_all_not_launched(monkeypatch):
    """No GPUs present → a GPU experiment can't be placed."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(5, requires_gpu=True)]
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_gpu_lane_serialized(monkeypatch):
    """MAX_GPU=1: with one GPU experiment already running, a second GPU experiment
    is not launched even though the host has VRAM headroom."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(6, requires_gpu=True)]
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": [_gpu(index=0, free_mb=40000.0)]}
    running = {5: (object(), True)}  # already one GPU experiment owning the lane

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()
    assert 6 not in running


@pytest.mark.asyncio
async def test_allocate_uses_explicit_gpu_mem_mb(monkeypatch):
    """A per-experiment gpu_mem_mb (below the clamp) is honoured over the default:
    a 7000MB need leaves no headroom on an 8000MB-free GPU, even though the 4096
    default would have fit → not launched."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    monkeypatch.setattr(qm, "GPU_MEM_DEFAULT", 4096)
    monkeypatch.setattr(qm, "GPU_MEM_MAX", 16384)  # high so the explicit 7000 isn't clamped
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    st.get_queued_experiments.return_value = [_exp(7, requires_gpu=True, gpu_mem_mb=7000)]
    # free 8000 - need 7000 = 1000 < reserve 2048 → no headroom (the 4096 default would have fit)
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": [_gpu(index=0, free_mb=8000.0)]}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []


@pytest.mark.asyncio
async def test_allocate_clamps_oversized_gpu_mem_to_max(monkeypatch):
    """An over-estimated gpu_mem_mb is clamped to GPU_MEM_MAX so the experiment
    stays schedulable: a 30000MB request is capped to 8192 and DOES launch on a
    12000MB-free GPU it would never fit unclamped."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    monkeypatch.setattr(qm, "GPU_MEM_MAX", 8192)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    exp = _exp(8, requires_gpu=True, gpu_mem_mb=30000)
    st.get_queued_experiments.return_value = [exp]
    # clamped need 8192: free 12000 - 8192 = 3808 >= reserve 2048 → fits
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": [_gpu(index=0, free_mb=12000.0)]}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == [8]
    assert exp["_gpu_device"] == "0"
    await _drain(running)


@pytest.mark.asyncio
async def test_allocate_defers_gpu_when_lab_busy(monkeypatch):
    """When the shared GPULock reads busy (the lab is actively serving the RAG /
    retrieval load on the GPU), a GPU experiment is deferred even with ample VRAM
    headroom — the live lab keeps GPU priority over the experiment lane."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    monkeypatch.setattr(qm, "MAX_GPU", 1)
    monkeypatch.setattr(qm, "GPU_MEM_DEFAULT", 4096)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    monkeypatch.setattr(qm, "shared_gpu_lock", lambda: types.SimpleNamespace(busy=lambda *a, **k: True))
    st = _state()
    st.get_queued_experiments.return_value = [_exp(9, requires_gpu=True)]
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": [_gpu(index=0, free_mb=40000.0)]}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_allocate_empty_queue_noop(monkeypatch):
    """No queued experiments → nothing launched, no claims."""
    monkeypatch.setattr(qm, "run_experiment", AsyncMock())
    st = _state()  # default get_queued_experiments returns []
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": []}
    running: dict = {}

    launched = await qm.allocate(st, AsyncMock(), AsyncMock(), res, running, {})

    assert launched == []
    st.mark_experiment_running.assert_not_awaited()


# ===========================================================================
# _age_s — heartbeat age math
# ===========================================================================
def test_age_s_none_for_none():
    assert qm._age_s(None) is None


def test_age_s_positive_for_past():
    past = datetime.now(UTC) - timedelta(seconds=120)
    age = qm._age_s(past)
    assert age is not None and 119 <= age <= 130


def test_age_s_swallows_bad_input():
    # naive datetime vs aware → subtraction raises → suppressed → None
    assert qm._age_s(datetime(2020, 1, 1)) is None  # noqa: DTZ001 — intentionally naive


# ===========================================================================
# sweep_kills — orphans, stalls, VRAM pressure
# ===========================================================================
@pytest.mark.asyncio
async def test_sweep_kills_orphan_row(monkeypatch):
    """A running DB row NOT in our `running` dict = an orphan from a prior harness:
    kill_experiment("orphaned…") + sandbox.force_remove on its container name."""
    sb = _patch_sandbox(monkeypatch)
    st = _state()
    st.get_running_experiments.return_value = [{"id": 9, "heartbeat_at": datetime.now(UTC)}]
    res = {"gpus": []}
    running: dict = {}
    kill_reasons: dict = {}

    await qm.sweep_kills(st, res, running, kill_reasons)

    st.kill_experiment.assert_awaited_once()
    args = st.kill_experiment.await_args.args
    assert args[0] == 9 and "orphaned" in args[1]
    sb.force_remove.assert_awaited_once_with(qm.sandbox.container_name(9))
    sb.kill.assert_not_awaited()
    assert kill_reasons == {}


@pytest.mark.asyncio
async def test_sweep_kills_stale_heartbeat(monkeypatch):
    """An owned running row whose heartbeat is older than budget + NO_PROGRESS_S is
    flagged in kill_reasons and the container is killed."""
    sb = _patch_sandbox(monkeypatch)
    monkeypatch.setattr(qm, "NO_PROGRESS_S", 180.0)
    st = _state()
    stale = datetime.now(UTC) - timedelta(seconds=2000)  # well past 600 + 180
    st.get_running_experiments.return_value = [{"id": 4, "heartbeat_at": stale, "wall_clock_budget_s": 600}]
    res = {"gpus": []}
    running = {4: (object(), False)}
    kill_reasons: dict = {}

    await qm.sweep_kills(st, res, running, kill_reasons)

    assert 4 in kill_reasons and "stalled" in kill_reasons[4]
    sb.kill.assert_awaited_once_with(4)
    st.kill_experiment.assert_not_awaited()  # session.run_code_session reads kill_reasons; no direct kill here


@pytest.mark.asyncio
async def test_sweep_kills_fresh_heartbeat_left_alone(monkeypatch):
    """An owned running row with a fresh heartbeat and no VRAM pressure is untouched."""
    sb = _patch_sandbox(monkeypatch)
    st = _state()
    st.get_running_experiments.return_value = [{"id": 4, "heartbeat_at": datetime.now(UTC), "wall_clock_budget_s": 600}]
    res = {"gpus": [_gpu(index=0, free_mb=40000.0)]}
    running = {4: (object(), False)}
    kill_reasons: dict = {}

    await qm.sweep_kills(st, res, running, kill_reasons)

    assert kill_reasons == {}
    sb.kill.assert_not_awaited()
    st.kill_experiment.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_kills_vram_pressure_kills_owned_gpu(monkeypatch):
    """A GPU below the reserve (VRAM pressure) + an owned GPU experiment → that
    experiment is flagged in kill_reasons and killed (protect Ollama)."""
    sb = _patch_sandbox(monkeypatch)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    st.get_running_experiments.return_value = [{"id": 8, "heartbeat_at": datetime.now(UTC), "wall_clock_budget_s": 600}]
    res = {"gpus": [_gpu(index=0, free_mb=100.0)]}  # 100 < reserve 2048 → pressure
    running = {8: (object(), True)}  # owned, and it's the GPU lane
    kill_reasons: dict = {}

    await qm.sweep_kills(st, res, running, kill_reasons)

    assert 8 in kill_reasons and "Ollama" in kill_reasons[8]
    sb.kill.assert_awaited_once_with(8)


@pytest.mark.asyncio
async def test_sweep_kills_vram_pressure_spares_cpu_experiment(monkeypatch):
    """VRAM pressure must NOT kill a CPU experiment (it isn't on the GPU lane)."""
    sb = _patch_sandbox(monkeypatch)
    monkeypatch.setattr(qm, "GPU_RESERVE_MB", 2048)
    st = _state()
    st.get_running_experiments.return_value = [{"id": 8, "heartbeat_at": datetime.now(UTC), "wall_clock_budget_s": 600}]
    res = {"gpus": [_gpu(index=0, free_mb=100.0)]}  # pressure
    running = {8: (object(), False)}  # CPU lane → not a candidate for VRAM kill
    kill_reasons: dict = {}

    await qm.sweep_kills(st, res, running, kill_reasons)

    assert kill_reasons == {}
    sb.kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_kills_empty_noop(monkeypatch):
    """No running rows → nothing killed."""
    sb = _patch_sandbox(monkeypatch)
    st = _state()  # default get_running_experiments returns []
    await qm.sweep_kills(st, {"gpus": []}, {}, {})
    st.kill_experiment.assert_not_awaited()
    sb.kill.assert_not_awaited()
    sb.force_remove.assert_not_awaited()


# ===========================================================================
# run_experiment — terminal outcomes (mock run_code_session)
# ===========================================================================
@pytest.mark.asyncio
async def test_run_experiment_completed(monkeypatch):
    """run_code_session returns completed → record_experiment_result(completed) +
    emit experiment.completed, and the id is popped from `running`."""
    monkeypatch.setattr(
        qm,
        "run_code_session",
        AsyncMock(return_value={"status": "completed", "result": {"acc": 0.9}, "meta": {"iterations": 1}}),
    )
    st = _state()
    exp = _exp(11)
    running = {11: (object(), False)}
    kill_reasons: dict = {}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), exp, running, kill_reasons)

    st.record_experiment_result.assert_awaited_once()
    kwargs = st.record_experiment_result.await_args.kwargs
    assert st.record_experiment_result.await_args.args[0] == 11
    assert kwargs["status"] == "completed" and kwargs["result"] == {"acc": 0.9}
    assert kwargs["resource_usage"] == {"iterations": 1}
    st.emit_corpus_event.assert_awaited_once()
    ev_type = st.emit_corpus_event.await_args.args[0]
    payload = st.emit_corpus_event.await_args.kwargs["payload"]
    assert ev_type == "experiment.completed"
    assert payload == {"experiment_id": 11, "claim_id": 7, "task_id": 3}
    assert 11 not in running  # finally-clause popped it


@pytest.mark.asyncio
async def test_run_experiment_failed(monkeypatch):
    """run_code_session returns failed → record_experiment_result(failed) +
    emit experiment.failed with the error."""
    monkeypatch.setattr(
        qm,
        "run_code_session",
        AsyncMock(return_value={"status": "failed", "error": "boom", "meta": {"attempts": []}}),
    )
    st = _state()
    running = {12: (object(), False)}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), _exp(12), running, {})

    kwargs = st.record_experiment_result.await_args.kwargs
    assert kwargs["status"] == "failed" and kwargs["error"] == "boom"
    assert st.emit_corpus_event.await_args.args[0] == "experiment.failed"
    payload = st.emit_corpus_event.await_args.kwargs["payload"]
    assert payload["error"] == "boom" and payload["experiment_id"] == 12
    assert 12 not in running


@pytest.mark.asyncio
async def test_run_experiment_killed_status(monkeypatch):
    """run_code_session returns killed → kill_experiment + emit experiment.failed
    with killed=True (no record_experiment_result on this path)."""
    monkeypatch.setattr(
        qm,
        "run_code_session",
        AsyncMock(return_value={"status": "killed", "error": "stalled", "meta": {}}),
    )
    st = _state()
    running = {13: (object(), False)}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), _exp(13), running, {})

    st.kill_experiment.assert_awaited_once()
    assert st.kill_experiment.await_args.args[0] == 13
    assert st.emit_corpus_event.await_args.args[0] == "experiment.failed"
    payload = st.emit_corpus_event.await_args.kwargs["payload"]
    assert payload["killed"] is True
    st.record_experiment_result.assert_not_awaited()
    assert 13 not in running


@pytest.mark.asyncio
async def test_run_experiment_killed_via_kill_reasons(monkeypatch):
    """Even with a non-killed status, a kill_reason recorded for this eid routes to
    the killed branch (the QM's sweep set the reason mid-run). kill_reasons is popped."""
    monkeypatch.setattr(
        qm,
        "run_code_session",
        AsyncMock(return_value={"status": "failed", "error": "ignored", "meta": {}}),
    )
    st = _state()
    running = {14: (object(), False)}
    kill_reasons = {14: "GPU VRAM headroom breached"}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), _exp(14), running, kill_reasons)

    st.kill_experiment.assert_awaited_once()
    assert st.kill_experiment.await_args.args[1] == "GPU VRAM headroom breached"
    assert st.emit_corpus_event.await_args.args[0] == "experiment.failed"
    assert st.emit_corpus_event.await_args.kwargs["payload"]["killed"] is True
    assert 14 not in kill_reasons  # consumed
    assert 14 not in running


@pytest.mark.asyncio
async def test_run_experiment_heartbeat_callback(monkeypatch):
    """The on_heartbeat passed into run_code_session beats the experiment row."""
    captured = {}

    async def _fake_session(state, router, curator, exp, *, on_heartbeat=None, kill_reasons=None):
        captured["beat"] = on_heartbeat
        await on_heartbeat()
        return {"status": "completed", "result": {}, "meta": {}}

    monkeypatch.setattr(qm, "run_code_session", _fake_session)
    st = _state()
    running = {15: (object(), False)}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), _exp(15), running, {})

    st.heartbeat_experiment.assert_awaited_once_with(15)
    assert callable(captured["beat"])


@pytest.mark.asyncio
async def test_run_experiment_runner_crash_records_failed(monkeypatch):
    """If run_code_session itself raises, the runner must not propagate: it records a
    failed result and still pops `running` (so a crash can't wedge the watchdog)."""
    monkeypatch.setattr(qm, "run_code_session", AsyncMock(side_effect=RuntimeError("kaboom")))
    st = _state()
    running = {16: (object(), False)}

    await qm.run_experiment(st, AsyncMock(), AsyncMock(), _exp(16), running, {})

    st.record_experiment_result.assert_awaited_once()
    kwargs = st.record_experiment_result.await_args.kwargs
    assert kwargs["status"] == "failed" and "crashed" in kwargs["error"]
    assert 16 not in running


# ===========================================================================
# reap_orphan_containers — remove lab containers we don't own
# ===========================================================================
@pytest.mark.asyncio
async def test_reap_orphan_containers_removes_unowned(monkeypatch):
    """Any lf-exp-* container not owned by this QM (a crashed-harness leftover) is
    force-removed; ours is left alone."""
    monkeypatch.setattr(qm.sandbox, "list_lab_containers", AsyncMock(return_value=["lf-exp-1", "lf-exp-99"]))
    monkeypatch.setattr(qm.sandbox, "force_remove", AsyncMock())
    running = {1: (object(), False)}  # we own lf-exp-1, not lf-exp-99

    await qm.reap_orphan_containers(_state(), running)

    qm.sandbox.force_remove.assert_awaited_once_with("lf-exp-99")


@pytest.mark.asyncio
async def test_reap_orphan_containers_noop_when_all_owned(monkeypatch):
    monkeypatch.setattr(qm.sandbox, "list_lab_containers", AsyncMock(return_value=["lf-exp-1"]))
    monkeypatch.setattr(qm.sandbox, "force_remove", AsyncMock())
    await qm.reap_orphan_containers(_state(), {1: (object(), False)})
    qm.sandbox.force_remove.assert_not_awaited()


# ===========================================================================
# _emit_snapshot — periodic resource event (best-effort)
# ===========================================================================
@pytest.mark.asyncio
async def test_emit_snapshot_emits_payload():
    st = _state()
    res = {"cpu_percent": 12.0, "mem_percent": 34.0, "disk_percent": 50.0, "gpus": [_gpu()]}
    running = {1: (object(), False), 2: (object(), True)}

    await qm._emit_snapshot(st, res, running)

    st.emit_corpus_event.assert_awaited_once()
    assert st.emit_corpus_event.await_args.args[0] == "quartermaster.snapshot"
    payload = st.emit_corpus_event.await_args.kwargs["payload"]
    assert payload["cpu_percent"] == 12.0 and payload["running_experiments"] == 2


@pytest.mark.asyncio
async def test_emit_snapshot_swallows_emit_error():
    """A DB hiccup emitting the snapshot must never propagate (it's best-effort)."""
    st = _state()
    st.emit_corpus_event.side_effect = RuntimeError("db down")
    await qm._emit_snapshot(st, {"gpus": []}, {})  # must not raise


# ===========================================================================
# quartermaster_watchdog — gating + one tick + clean stop
# ===========================================================================
@pytest.mark.asyncio
async def test_watchdog_disabled_returns_immediately(monkeypatch):
    """With the QUARTERMASTER flag off, the watchdog returns without touching the DB."""
    monkeypatch.delenv("QUARTERMASTER", raising=False)
    made = {"client": 0}
    monkeypatch.setattr(qm, "PostgresClient", lambda **kw: made.__setitem__("client", made["client"] + 1))
    await qm.quartermaster_watchdog(object(), asyncio.Event())
    assert made["client"] == 0


@pytest.mark.asyncio
async def test_watchdog_runs_one_tick_then_stops(monkeypatch):
    """Enabled + mode 'active': one tick samples resources, emits a snapshot, sweeps
    and allocates, then the stop event ends the loop."""
    monkeypatch.setenv("QUARTERMASTER", "on")
    monkeypatch.setattr(qm, "INTERVAL_S", 0.001)
    st = _state()
    monkeypatch.setattr(qm, "PostgresClient", lambda **kw: st)
    monkeypatch.setattr(qm, "get_agent_mode", AsyncMock(return_value="active"))
    res = {"cpu_percent": 1.0, "mem_percent": 1.0, "gpus": []}
    monkeypatch.setattr(qm, "sample_resources", AsyncMock(return_value=res))
    monkeypatch.setattr(qm, "reap_orphan_containers", AsyncMock())
    monkeypatch.setattr(qm, "_emit_snapshot", AsyncMock())
    monkeypatch.setattr(qm, "sweep_kills", AsyncMock())

    stop = asyncio.Event()
    calls = {"n": 0}

    async def _alloc(*a, **k):
        calls["n"] += 1
        stop.set()  # end the loop after the first allocate
        return []

    monkeypatch.setattr(qm, "allocate", _alloc)

    await asyncio.wait_for(qm.quartermaster_watchdog(object(), stop, router="r", curator="c"), timeout=2)

    assert calls["n"] >= 1
    qm.reap_orphan_containers.assert_awaited_once()
    qm.sweep_kills.assert_awaited()
    qm._emit_snapshot.assert_awaited()


@pytest.mark.asyncio
async def test_watchdog_mode_gate_skips_work(monkeypatch):
    """A non-advisory/active mode pauses the QM: the tick samples nothing and just
    loops until stop. (We stop it from the mode probe to bound the test.)"""
    monkeypatch.setenv("QUARTERMASTER", "on")
    monkeypatch.setattr(qm, "INTERVAL_S", 0.001)
    st = _state()
    monkeypatch.setattr(qm, "PostgresClient", lambda **kw: st)
    monkeypatch.setattr(qm, "reap_orphan_containers", AsyncMock())
    monkeypatch.setattr(qm, "sample_resources", AsyncMock())
    stop = asyncio.Event()

    async def _mode(pool, agent):
        stop.set()  # bound the loop: stop right after the gate check
        return "paused"

    monkeypatch.setattr(qm, "get_agent_mode", _mode)

    await asyncio.wait_for(qm.quartermaster_watchdog(object(), stop), timeout=2)

    qm.sample_resources.assert_not_awaited()  # mode gate short-circuited before sampling


@pytest.mark.asyncio
async def test_watchdog_stops_before_first_tick(monkeypatch):
    """If stop is already set, the loop exits on the first wait without a tick."""
    monkeypatch.setenv("QUARTERMASTER", "on")
    monkeypatch.setattr(qm, "INTERVAL_S", 5.0)
    st = _state()
    monkeypatch.setattr(qm, "PostgresClient", lambda **kw: st)
    monkeypatch.setattr(qm, "reap_orphan_containers", AsyncMock())
    monkeypatch.setattr(qm, "get_agent_mode", AsyncMock(return_value="active"))
    monkeypatch.setattr(qm, "sample_resources", AsyncMock())

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(qm.quartermaster_watchdog(object(), stop), timeout=2)
    qm.sample_resources.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_tick_error_does_not_kill_loop(monkeypatch):
    """A bad tick (sample_resources raises) is logged and swallowed; the loop keeps
    going and stops cleanly on the event."""
    monkeypatch.setenv("QUARTERMASTER", "on")
    monkeypatch.setattr(qm, "INTERVAL_S", 0.001)
    st = _state()
    monkeypatch.setattr(qm, "PostgresClient", lambda **kw: st)
    monkeypatch.setattr(qm, "get_agent_mode", AsyncMock(return_value="active"))
    monkeypatch.setattr(qm, "reap_orphan_containers", AsyncMock())

    stop = asyncio.Event()
    calls = {"n": 0}

    async def _sample():
        calls["n"] += 1
        stop.set()  # bound the loop after the raising tick
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(qm, "sample_resources", _sample)

    await asyncio.wait_for(qm.quartermaster_watchdog(object(), stop), timeout=2)  # must not raise
    assert calls["n"] >= 1
