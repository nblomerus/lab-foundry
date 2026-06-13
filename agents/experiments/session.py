"""The experiment coding loop — run the generated code, and when it fails,
DEBUG it and retry, like a coding agent driving an experiment to a usable result.

This is the iterative half of the experiments harness. The Quartermaster owns it
(runs it on its own compute pool, off the dispatcher's handler slots, with the
router/curator), so the multiple sandbox runs + `experiments.debug` LLM calls
never wedge the event loop, and the QM enforces the SESSION budget (total
wall-clock across attempts) and can kill a session that won't converge.

`run_code_session` returns the terminal outcome; the Quartermaster records it +
emits `experiment.completed` / `experiment.failed` (which the experiments agent's
handlers then interpret into confidence feedback + a Library note).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable

from agents.experiments import sandbox
from agents.experiments.schemas import ExperimentDesign

log = logging.getLogger(__name__)

MAX_ITERS = int(os.environ.get("EXPERIMENT_MAX_ITERS", "5"))  # design run + up to N debug retries
PER_RUN_CAP_S = int(os.environ.get("EXPERIMENT_PER_RUN_S", "300"))  # per-container wall-clock cap
CPUS = float(os.environ.get("EXPERIMENT_CPUS", "1.0"))


async def _claim_statement(state, claim_id) -> str:
    if claim_id is None:
        return ""
    try:
        claim = await state.get_claim(claim_id)
        return claim.statement
    except Exception:  # noqa: BLE001 — inactive/missing claim is non-fatal context
        return ""


async def run_code_session(
    state,
    router,
    curator,
    exp: dict,
    *,
    on_heartbeat: Callable[[], Awaitable[None]] | None = None,
    kill_reasons: dict | None = None,
) -> dict:
    """Run → debug → retry until the experiment produces a result or the budget is
    spent. Returns {status: completed|failed|killed, result, error, meta:{iterations, attempts, usage}}."""
    kill_reasons = kill_reasons if kill_reasons is not None else {}
    eid = exp["id"]
    params = exp.get("params") or {}
    claim_id = params.get("claim_id")
    hypothesis = params.get("hypothesis") or ""
    claim_statement = await _claim_statement(state, claim_id)
    code = exp.get("code") or ""
    requires_gpu = bool(exp.get("requires_gpu"))
    gpu_device = exp.get("_gpu_device")
    mem_mb = int(exp.get("mem_budget_mb") or 2048)
    session_budget_s = int(exp.get("wall_clock_budget_s") or 1200)
    start = time.monotonic()
    attempts: list[dict] = []

    if not code:
        return {"status": "failed", "error": "no code on the experiment row", "meta": {"iterations": 0, "attempts": []}}

    for iteration in range(1, MAX_ITERS + 1):
        remaining = session_budget_s - (time.monotonic() - start)
        if remaining <= 10:
            return {
                "status": "failed",
                "error": f"session budget {session_budget_s}s exhausted after {iteration - 1} attempt(s)",
                "meta": {"iterations": iteration - 1, "attempts": attempts},
            }
        per_run = int(min(remaining, PER_RUN_CAP_S))
        sb = await sandbox.run_in_container(
            eid,
            code,
            wall_clock_s=per_run,
            mem_mb=mem_mb,
            cpus=CPUS,
            requires_gpu=requires_gpu,
            gpu_device=gpu_device,
            on_heartbeat=on_heartbeat,
        )
        if sb.status == "completed":
            with contextlib.suppress(Exception):
                await state.update_experiment_code(eid, code)  # persist the final WORKING code
            return {
                "status": "completed",
                "result": sb.result,
                "error": None,
                "meta": {"iterations": iteration, "attempts": attempts, "usage": sb.usage},
            }

        attempts.append({"iteration": iteration, "status": sb.status, "error": (sb.error or "")[:1500]})

        # The Quartermaster actively killed this experiment (session budget / VRAM / stalled) → stop.
        if eid in kill_reasons:
            return {
                "status": "killed",
                "error": kill_reasons.get(eid),
                "meta": {"iterations": iteration, "attempts": attempts},
            }
        if iteration >= MAX_ITERS:
            break

        # DEBUG: read the code + the failure, fix it, retry.
        try:
            prompt = await curator.build(
                invocation_type="experiments.debug",
                context={
                    "code": code,
                    "error": sb.error or "the script produced no JSON result",
                    "hypothesis": hypothesis,
                    "claim_statement": claim_statement,
                    "iteration": iteration,
                },
            )
            fix, _run_id = await router.invoke(
                prompt=prompt,
                output_schema_class=ExperimentDesign,
                triggered_by_event_id=None,
                session=None,
                step_name="experiments.debug",
            )
            code = fix.code
            requires_gpu = requires_gpu or bool(fix.requires_gpu)
            with contextlib.suppress(Exception):
                await state.update_experiment_code(eid, code)
            log.info("experiments: debug attempt %d for exp %s", iteration, eid)
        except Exception as e:  # noqa: BLE001 — a debug LLM failure ends the session as failed
            log.exception("experiments: debug step failed for exp %s", eid)
            return {
                "status": "failed",
                "error": f"debug step failed: {e}",
                "meta": {"iterations": iteration, "attempts": attempts},
            }

    return {
        "status": "failed",
        "error": (attempts[-1]["error"] if attempts else "no result") + f" (gave up after {len(attempts)} attempts)",
        "meta": {"iterations": MAX_ITERS, "attempts": attempts},
    }
