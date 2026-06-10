"""Tests for the experiment coding loop — agents/experiments/session.py.

`run_code_session` drives a generated experiment to a usable result: run it in
the sandbox, and when it fails, DEBUG it via the router and retry, up to
MAX_ITERS, while honouring kill signals and the session budget.

Everything is mocked: `state` is an AsyncMock, `sandbox.run_in_container` is
monkeypatched to an AsyncMock returning a real `SandboxResult`, and the
router/curator are AsyncMocks (no Docker, no DB, no network, no LLM).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.experiments import session as sess
from agents.experiments.sandbox import SandboxResult
from agents.experiments.schemas import ExperimentDesign


def _state() -> AsyncMock:
    st = AsyncMock()
    st.get_claim.return_value = SimpleNamespace(statement="claim under test")
    return st


def _router_curator(design: ExperimentDesign | None = None) -> tuple[AsyncMock, AsyncMock]:
    """router.invoke -> (ExperimentDesign, run_id); curator.build -> "PROMPT"."""
    if design is None:
        design = ExperimentDesign(code="print('{}')", hypothesis="h", requires_gpu=False)
    router = AsyncMock()
    router.invoke.return_value = (design, 1)
    curator = AsyncMock()
    curator.build.return_value = "PROMPT"
    return router, curator


def _exp(**overrides) -> dict:
    exp = {
        "id": 42,
        "code": "print('{\"acc\": 1.0}')",
        "params": {"claim_id": 7, "hypothesis": "deeper nets help"},
        "requires_gpu": False,
        "mem_budget_mb": 1024,
        "wall_clock_budget_s": 1200,
    }
    exp.update(overrides)
    return exp


@pytest.fixture(autouse=True)
def _fast_caps(monkeypatch):
    """Keep the loop deterministic and bounded: small per-run cap + iters."""
    monkeypatch.setattr(sess, "PER_RUN_CAP_S", 30, raising=True)
    monkeypatch.setattr(sess, "MAX_ITERS", 5, raising=True)


def _patch_sandbox(monkeypatch, results) -> AsyncMock:
    """Monkeypatch sandbox.run_in_container to an AsyncMock returning `results`
    (a single SandboxResult, or a list consumed in sequence via side_effect)."""
    mock = AsyncMock()
    if isinstance(results, list):
        mock.side_effect = results
    else:
        mock.return_value = results
    monkeypatch.setattr(sess.sandbox, "run_in_container", mock)
    return mock


# ── success on first run ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_success_first_run(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(
        monkeypatch,
        SandboxResult("completed", {"acc": 0.9}, None, {"exit_code": 0, "duration_s": 1.0}),
    )

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "completed"
    assert out["result"] == {"acc": 0.9}
    assert out["error"] is None
    assert out["meta"]["iterations"] == 1
    assert out["meta"]["attempts"] == []
    assert out["meta"]["usage"] == {"exit_code": 0, "duration_s": 1.0}
    # Exactly one sandbox run, and no debug LLM call.
    assert sb.await_count == 1
    router.invoke.assert_not_awaited()
    curator.build.assert_not_awaited()
    # Final working code persisted.
    state.update_experiment_code.assert_awaited_once()
    # Claim statement was looked up for context.
    state.get_claim.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_success_passes_heartbeat_and_gpu(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(monkeypatch, SandboxResult("completed", {"ok": True}, None, {}))

    async def _hb():
        return None

    exp = _exp(requires_gpu=True, _gpu_device="0")
    out = await sess.run_code_session(state, router, curator, exp, on_heartbeat=_hb)

    assert out["status"] == "completed"
    _, kwargs = sb.await_args
    assert kwargs["requires_gpu"] is True
    assert kwargs["gpu_device"] == "0"
    assert kwargs["on_heartbeat"] is _hb
    assert kwargs["mem_mb"] == 1024


# ── fail then success ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fail_then_success(monkeypatch):
    state = _state()
    fixed = ExperimentDesign(code="print('{\"acc\": 1.0}')  # fixed", hypothesis="h")
    router, curator = _router_curator(fixed)
    sb = _patch_sandbox(
        monkeypatch,
        [
            SandboxResult("failed", None, "Traceback: NameError", {"exit_code": 1}),
            SandboxResult("completed", {"acc": 1.0}, None, {"exit_code": 0}),
        ],
    )

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "completed"
    assert out["result"] == {"acc": 1.0}
    assert out["meta"]["iterations"] == 2
    assert len(out["meta"]["attempts"]) == 1
    assert out["meta"]["attempts"][0]["iteration"] == 1
    assert out["meta"]["attempts"][0]["status"] == "failed"
    assert "NameError" in out["meta"]["attempts"][0]["error"]
    assert sb.await_count == 2
    # One debug round-trip happened.
    curator.build.assert_awaited_once()
    router.invoke.assert_awaited_once()
    # The router was asked for an ExperimentDesign.
    _, inv_kwargs = router.invoke.await_args
    assert inv_kwargs["output_schema_class"] is ExperimentDesign
    assert inv_kwargs["step_name"] == "experiments.debug"
    # Second sandbox run used the DEBUGGED code, and the fixed code was persisted.
    second_call_code = sb.await_args_list[1].args[1]
    assert second_call_code == fixed.code
    assert state.update_experiment_code.await_count == 2


@pytest.mark.asyncio
async def test_debug_gpu_flag_sticks(monkeypatch):
    """If the debug fix flips requires_gpu on, the retry run requests a GPU."""
    state = _state()
    fixed = ExperimentDesign(code="print('{}')", hypothesis="h", requires_gpu=True)
    router, curator = _router_curator(fixed)
    sb = _patch_sandbox(
        monkeypatch,
        [
            SandboxResult("failed", None, "needs cuda", {}),
            SandboxResult("completed", {"ok": 1}, None, {}),
        ],
    )

    out = await sess.run_code_session(state, router, curator, _exp(requires_gpu=False))

    assert out["status"] == "completed"
    assert sb.await_args_list[0].kwargs["requires_gpu"] is False
    assert sb.await_args_list[1].kwargs["requires_gpu"] is True


# ── give up after MAX_ITERS ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_give_up_after_max_iters(monkeypatch):
    monkeypatch.setattr(sess, "MAX_ITERS", 2, raising=True)
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(
        monkeypatch,
        [
            SandboxResult("failed", None, "boom one", {"exit_code": 1}),
            SandboxResult("failed", None, "boom two", {"exit_code": 1}),
        ],
    )

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "failed"
    assert out["meta"]["iterations"] == 2
    assert len(out["meta"]["attempts"]) == 2
    assert "boom two" in out["error"]
    assert "gave up after 2 attempts" in out["error"]
    assert sb.await_count == 2
    # One debug between the two attempts (none after the last).
    assert router.invoke.await_count == 1


# ── kill ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_kill_after_failed_run(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(
        monkeypatch,
        SandboxResult("failed", None, "still failing", {"exit_code": 1}),
    )
    exp = _exp()
    kill_reasons = {exp["id"]: "session budget exceeded"}

    out = await sess.run_code_session(state, router, curator, exp, kill_reasons=kill_reasons)

    assert out["status"] == "killed"
    assert out["error"] == "session budget exceeded"
    assert out["meta"]["iterations"] == 1
    assert len(out["meta"]["attempts"]) == 1
    # Killed BEFORE any debug attempt.
    router.invoke.assert_not_awaited()
    assert sb.await_count == 1


# ── no code on the row ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_code(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(monkeypatch, SandboxResult("completed", {}, None, {}))

    out = await sess.run_code_session(state, router, curator, _exp(code=""))

    assert out["status"] == "failed"
    assert out["error"].startswith("no code")
    assert out["meta"] == {"iterations": 0, "attempts": []}
    # Never touched the sandbox or the router.
    sb.assert_not_awaited()
    router.invoke.assert_not_awaited()


# ── debug LLM raising ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_debug_step_raises(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    router.invoke.side_effect = RuntimeError("router exploded")
    sb = _patch_sandbox(
        monkeypatch,
        SandboxResult("failed", None, "first failure", {"exit_code": 1}),
    )

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "failed"
    assert out["error"].startswith("debug step failed")
    assert "router exploded" in out["error"]
    assert out["meta"]["iterations"] == 1
    assert len(out["meta"]["attempts"]) == 1
    assert sb.await_count == 1


@pytest.mark.asyncio
async def test_curator_build_raises_is_debug_failure(monkeypatch):
    """A curator failure during the debug step is caught the same way."""
    state = _state()
    router, curator = _router_curator()
    curator.build.side_effect = ValueError("no prompt")
    _patch_sandbox(monkeypatch, SandboxResult("failed", None, "oops", {}))

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "failed"
    assert out["error"].startswith("debug step failed")
    assert "no prompt" in out["error"]
    router.invoke.assert_not_awaited()


# ── claim-lookup edge paths ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_claim_id_skips_lookup(monkeypatch):
    state = _state()
    router, curator = _router_curator()
    _patch_sandbox(monkeypatch, SandboxResult("completed", {"ok": 1}, None, {}))

    out = await sess.run_code_session(state, router, curator, _exp(params={"hypothesis": "h"}))

    assert out["status"] == "completed"
    state.get_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_lookup_failure_is_non_fatal(monkeypatch):
    state = _state()
    state.get_claim.side_effect = RuntimeError("claim gone")
    router, curator = _router_curator()
    _patch_sandbox(monkeypatch, SandboxResult("completed", {"ok": 1}, None, {}))

    out = await sess.run_code_session(state, router, curator, _exp())

    assert out["status"] == "completed"
    state.get_claim.assert_awaited_once()


# ── session budget exhausted ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_session_budget_exhausted_before_first_run(monkeypatch):
    """A wall_clock_budget_s <= 10 means there's no room to run; bail immediately."""
    state = _state()
    router, curator = _router_curator()
    sb = _patch_sandbox(monkeypatch, SandboxResult("completed", {}, None, {}))

    out = await sess.run_code_session(state, router, curator, _exp(wall_clock_budget_s=5))

    assert out["status"] == "failed"
    assert "session budget 5s exhausted" in out["error"]
    assert out["meta"] == {"iterations": 0, "attempts": []}
    sb.assert_not_awaited()
