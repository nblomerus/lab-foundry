"""The Quartermaster — the lab's resource manager for sandboxed experiments.

A condition-driven background watchdog (the `ariadne_pacemaker` template) that
each tick: MONITORS live compute (CPU/mem/GPU via ops.resources), ALLOCATES
queued experiments onto free capacity (a CPU lane and a serialized, VRAM-gated
GPU lane that never starves Ollama), and KILLS dead/over-budget/orphaned ones
(`docker kill`). It OWNS the experiment execution pool — running a 10-minute
container on its own asyncio tasks, never a dispatcher handler slot — so a long
experiment can't wedge the event loop, and the QM can terminate any of them.

Gated on the `QUARTERMASTER` env flag + the `quartermaster` mode dial. Results
flow back as `experiment.completed` / `experiment.failed` events the experiments
agent interprets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime

from agents.experiments import sandbox
from agents.experiments.session import run_code_session
from harness.agent_modes import get_agent_mode
from ops.resources import best_gpu_with_headroom, sample_resources
from state.client import PostgresClient

log = logging.getLogger(__name__)

INTERVAL_S = float(os.environ.get("QUARTERMASTER_INTERVAL_S", "30"))
MAX_CPU = int(os.environ.get("MAX_CONCURRENT_EXPERIMENTS", "2"))
MAX_GPU = int(os.environ.get("MAX_CONCURRENT_GPU_EXPERIMENTS", "1"))
CPUS = float(os.environ.get("EXPERIMENT_CPUS", "1.0"))
GPU_MEM_DEFAULT = int(os.environ.get("EXPERIMENT_GPU_MEM_MB", "4096"))
GPU_RESERVE_MB = int(os.environ.get("EXPERIMENT_GPU_HEADROOM_MB", "2048"))
CPU_HEADROOM_PCT = float(os.environ.get("EXPERIMENT_CPU_HEADROOM_PCT", "80"))
MEM_HEADROOM_PCT = float(os.environ.get("EXPERIMENT_MEM_HEADROOM_PCT", "85"))
NO_PROGRESS_S = float(os.environ.get("EXPERIMENT_NO_PROGRESS_S", "180"))


def _enabled() -> bool:
    return os.environ.get("QUARTERMASTER", "").lower() in {"on", "1", "true", "yes"}


def _claim_id(exp: dict) -> int | None:
    p = exp.get("params") or {}
    return p.get("claim_id") if isinstance(p, dict) else None


async def run_experiment(state, router, curator, exp: dict, running: dict, kill_reasons: dict) -> None:
    """Drive the experiment's coding loop (design's code → run → debug → retry) on
    this QM task, then record the terminal outcome + emit experiment.completed/failed
    (which the experiments agent interprets into confidence feedback + a Library note).
    The single writer of an experiment's final status for QM-launched runs."""
    eid = exp["id"]
    claim_id = _claim_id(exp)

    async def _beat() -> None:
        await state.heartbeat_experiment(eid)

    try:
        out = await run_code_session(state, router, curator, exp, on_heartbeat=_beat, kill_reasons=kill_reasons)
        reason = kill_reasons.pop(eid, None)
        meta = out.get("meta") or {}
        status = out.get("status")
        if status == "completed":
            await state.record_experiment_result(eid, status="completed", result=out.get("result"), resource_usage=meta)
            await state.emit_corpus_event(
                "experiment.completed",
                target_type="experiment",
                target_id=eid,
                payload={"experiment_id": eid, "claim_id": claim_id, "task_id": exp.get("task_id")},
                dedup_key=f"exp-done-{eid}",
            )
        elif status == "killed" or reason is not None:
            await state.kill_experiment(eid, reason or out.get("error") or "killed (budget/headroom)")
            await state.emit_corpus_event(
                "experiment.failed",
                target_type="experiment",
                target_id=eid,
                payload={
                    "experiment_id": eid,
                    "claim_id": claim_id,
                    "killed": True,
                    "reason": reason or out.get("error"),
                },
                dedup_key=f"exp-fail-{eid}",
            )
        else:
            await state.record_experiment_result(eid, status="failed", error=out.get("error"), resource_usage=meta)
            await state.emit_corpus_event(
                "experiment.failed",
                target_type="experiment",
                target_id=eid,
                payload={"experiment_id": eid, "claim_id": claim_id, "error": out.get("error")},
                dedup_key=f"exp-fail-{eid}",
            )
    except Exception:  # noqa: BLE001 — a runner crash must never kill the watchdog
        log.exception("quartermaster: run_experiment %s crashed", eid)
        with contextlib.suppress(Exception):
            await state.record_experiment_result(eid, status="failed", error="runner crashed")
    finally:
        running.pop(eid, None)


async def allocate(state, router, curator, res: dict, running: dict, kill_reasons: dict) -> list[int]:
    """Promote queued experiments to running when there's capacity + headroom.
    CPU lane gated on host headroom; GPU lane serialized + VRAM-gated (reserving
    headroom for Ollama). Returns the experiment ids launched this tick."""
    queued = await state.get_queued_experiments(limit=20)
    cpu_running = sum(1 for v in running.values() if not v[1])
    gpu_running = sum(1 for v in running.values() if v[1])
    launched: list[int] = []
    for exp in queued:
        eid = exp["id"]
        if eid in running:
            continue
        is_gpu = bool(exp.get("requires_gpu"))
        if is_gpu:
            if gpu_running >= MAX_GPU:
                continue
            need = int(exp.get("gpu_mem_mb") or GPU_MEM_DEFAULT)
            device = best_gpu_with_headroom(res.get("gpus") or [], need, GPU_RESERVE_MB)
            if device is None:
                continue  # no GPU with VRAM headroom beyond Ollama's footprint
            exp["_gpu_device"] = str(device)
        else:
            if cpu_running >= MAX_CPU:
                continue
            if (res.get("cpu_percent") or 0) >= CPU_HEADROOM_PCT:
                continue
            if (res.get("mem_percent") or 0) >= MEM_HEADROOM_PCT:
                continue
        if not await state.mark_experiment_running(eid, sandbox.container_name(eid)):
            continue  # lost the race / already taken
        task = asyncio.create_task(run_experiment(state, router, curator, exp, running, kill_reasons))
        running[eid] = (task, is_gpu)
        launched.append(eid)
        if is_gpu:
            gpu_running += 1
        else:
            cpu_running += 1
    return launched


def _age_s(ts) -> float | None:
    if ts is None:
        return None
    with contextlib.suppress(Exception):
        return (datetime.now(UTC) - ts).total_seconds()
    return None


async def sweep_kills(state, res: dict, running: dict, kill_reasons: dict) -> None:
    """Kill running experiments that are orphaned (from a prior harness), stalled
    (no heartbeat), or breaching GPU VRAM (protect Ollama)."""
    rows = await state.get_running_experiments()
    # GPU VRAM protection: if any GPU is below the reserve, kill the GPU experiment we own.
    vram_pressure = any(
        (g.get("mem_free_mb") is not None and g["mem_free_mb"] < GPU_RESERVE_MB) for g in (res.get("gpus") or [])
    )
    for row in rows:
        eid = row["id"]
        owned = eid in running
        if not owned:
            # A 'running' row this QM did not launch = orphan from a previous harness incarnation.
            await state.kill_experiment(eid, "orphaned by harness restart")
            await sandbox.force_remove(sandbox.container_name(eid))
            continue
        budget = float(row.get("wall_clock_budget_s") or 600)
        age = _age_s(row.get("heartbeat_at"))
        if age is not None and age > budget + NO_PROGRESS_S:
            kill_reasons[eid] = f"no heartbeat for {int(age)}s (stalled)"
            await sandbox.kill(eid)
            continue
        if vram_pressure and running[eid][1]:
            kill_reasons[eid] = "GPU VRAM headroom breached — protecting Ollama"
            await sandbox.kill(eid)


async def reap_orphan_containers(state, running: dict) -> None:
    """Force-remove any lf-exp-* container not owned by this QM (left by a crash)."""
    names = await sandbox.list_lab_containers()
    owned = {sandbox.container_name(eid) for eid in running}
    for name in names:
        if name not in owned:
            await sandbox.force_remove(name)


async def _emit_snapshot(state, res: dict, running: dict) -> None:
    minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    payload = {
        "cpu_percent": res.get("cpu_percent"),
        "mem_percent": res.get("mem_percent"),
        "disk_percent": res.get("disk_percent"),
        "gpus": res.get("gpus"),
        "running_experiments": len(running),
    }
    with contextlib.suppress(Exception):
        await state.emit_corpus_event(
            "quartermaster.snapshot",
            target_type="agent",
            target_id=0,
            payload=payload,
            dedup_key=f"qm-snap-{minute}",
        )
    log.debug("quartermaster snapshot: %s", json.dumps(payload)[:300])


async def quartermaster_watchdog(pool, stop: asyncio.Event, router=None, curator=None) -> None:
    if not _enabled():
        return
    state = PostgresClient(pool=pool)
    running: dict[int, tuple[asyncio.Task, bool]] = {}
    kill_reasons: dict[int, str] = {}
    log.info(
        "quartermaster started (interval=%.0fs, max_cpu=%d, max_gpu=%d, gpu_reserve=%dMB)",
        INTERVAL_S,
        MAX_CPU,
        MAX_GPU,
        GPU_RESERVE_MB,
    )
    with contextlib.suppress(Exception):
        await reap_orphan_containers(state, running)  # clean any containers a crashed harness left behind
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_S)
            break  # stop was set
        except TimeoutError:
            pass  # a tick elapsed
        try:
            if await get_agent_mode(pool, "quartermaster") not in {"advisory", "active"}:
                continue  # the mode dial pauses the Quartermaster
            res = await sample_resources()
            await _emit_snapshot(state, res, running)
            await sweep_kills(state, res, running, kill_reasons)
            await allocate(state, router, curator, res, running, kill_reasons)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the watchdog
            log.exception("quartermaster: tick failed")
    # graceful drain: stop launching; let in-flight runs finish or be cancelled by shutdown
    log.info("quartermaster stopped (%d experiment(s) in flight)", len(running))
