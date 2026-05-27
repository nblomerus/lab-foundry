"""
Harness entry point.

Run after bootstrap:

    python -m src.harness.main

Wires Postgres pool, state/memory/lessons clients, Curator, Router,
Dispatcher; registers handlers; runs forever until SIGINT/SIGTERM.

The Dispatcher gets state/memory/curator/router attached after construction
so handlers can reach them through their `dispatcher` parameter.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress

import asyncpg
import httpx

from boardroom.harness.curator import Curator
from boardroom.harness.dispatch import Dispatcher
from boardroom.harness.router import GPULock, Router, build_cloud_chain
from boardroom.state.client import PostgresClient
from boardroom.memory.client import ZepClient
from boardroom.skills.client import LessonsClient
from boardroom.handlers.task_completed         import handle_task_completed
from boardroom.handlers.researcher             import handle_task_created
from boardroom.handlers.adversary              import handle_finding_high_signal
from boardroom.handlers.thesis_invalidated     import handle_thesis_invalidated
from boardroom.handlers.queue_empty            import handle_queue_empty
from boardroom.handlers.phase_adjudicator      import handle_thesis_confidence_changed
from boardroom.handlers.phase_transition       import handle_phase_transition_proposed
from boardroom.handlers.reflection             import handle_reflection_requested
from boardroom.handlers.audit_slop_detected    import handle_audit_slop_detected
from boardroom.handlers.phase_budget_exceeded  import handle_phase_budget_exceeded


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("harness")


# Sessions Zep must contain (created lazily on first use, but eager is cleaner)
ZEP_SESSIONS = [
    "theses-lifecycle",
    "phase-transitions",
    "ceo-deliberations",
    "dissent",
    "charter",
]


async def _preflight(pool, ollama_url: str, memory: ZepClient) -> bool:
    """
    Verify external dependencies before entering the dispatch loop.

    Returns False (fatal) only if a hard dependency — Postgres or Ollama — is
    unreachable; the loop can do no useful work without them. Zep is checked
    too, but a Zep problem is logged loudly and tolerated: episodic memory is
    a nice-to-have, and we'd rather run degraded than not at all.
    """
    ok = True

    # Postgres — fatal.
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        log.info("preflight: postgres OK")
    except Exception as e:
        log.error("preflight: postgres unreachable: %s", e)
        ok = False

    # Ollama — fatal. No model server, no work.
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{ollama_url}/api/version")
            r.raise_for_status()
        log.info("preflight: ollama OK (%s)", ollama_url)
    except Exception as e:
        log.error("preflight: ollama unreachable at %s: %s", ollama_url, e)
        ok = False

    # Zep — non-fatal, but a broken API shape (e.g. the memory->thread rename)
    # is exactly the silent failure we want to catch at boot.
    try:
        await memory.ping()
        log.info("preflight: zep OK")
    except Exception as e:
        log.error("preflight: ZEP DEGRADED — memory writes/recall will fail: %s", e)

    return ok


async def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    max_concurrent = int(os.environ.get("MAX_CONCURRENT_HANDLERS", "4"))
    gpu_call_timeout = float(os.environ.get("GPU_CALL_TIMEOUT_S", "300"))
    cloud_chain = build_cloud_chain(os.environ)

    log.info("starting harness")
    log.info("  db=%s", db_url)
    log.info("  ollama=%s", ollama_url)
    log.info("  max_concurrent_handlers=%d", max_concurrent)
    log.info("  gpu_call_timeout_s=%.0f", gpu_call_timeout)
    if cloud_chain:
        log.info("  cloud chain: %s → local", " → ".join(
            f"{cp.provider.value}({cp.model_name})" for cp in cloud_chain))
    else:
        log.info("  cloud chain: (none — all-local)")

    pool = await asyncpg.create_pool(db_url, min_size=4, max_size=20)

    # Bootstrap check
    async with pool.acquire() as conn:
        bootstrapped = await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1")
    if not bootstrapped:
        log.error("company_state not seeded; run `python -m src.bootstrap` first")
        await pool.close()
        return 1

    # Build clients and components
    state   = PostgresClient(pool=pool)
    memory  = ZepClient.from_env()
    lessons = LessonsClient(pool=pool)

    # Preflight: fail loud now if a hard dependency is broken, rather than
    # degrading silently mid-run (the Zep memory->thread break failed 82
    # events before anyone noticed). DB + Ollama are fatal; Zep is non-fatal
    # (memory is a nice-to-have) but a broken API shape is logged loudly.
    if not await _preflight(pool, ollama_url, memory):
        await pool.close()
        return 1

    await memory.ensure_user()
    for session in ZEP_SESSIONS:
        await memory.ensure_session(session)

    curator    = Curator(state=state, memory=memory, lessons=lessons)
    gpu_lock   = GPULock()
    router     = Router(pool=pool, gpu_lock=gpu_lock, ollama_url=ollama_url,
                        call_timeout_s=gpu_call_timeout, cloud_chain=cloud_chain)
    dispatcher = Dispatcher(pool=pool, max_concurrent_handlers=max_concurrent)

    # Attach clients so handlers can reach them via the dispatcher param
    dispatcher.state   = state
    dispatcher.memory  = memory
    dispatcher.lessons = lessons
    dispatcher.curator = curator
    dispatcher.router  = router

    # Register handlers — covers the full exploration → execution loop
    dispatcher.register("task.created",              handle_task_created)
    dispatcher.register("task.completed",            handle_task_completed)
    dispatcher.register("finding.high_signal",       handle_finding_high_signal)
    dispatcher.register("thesis.invalidated",        handle_thesis_invalidated)
    dispatcher.register("queue.empty",               handle_queue_empty)
    dispatcher.register("thesis.confidence_changed", handle_thesis_confidence_changed)
    dispatcher.register("phase.transition_proposed", handle_phase_transition_proposed)
    dispatcher.register("reflection.requested",      handle_reflection_requested)
    dispatcher.register("audit.slop_detected",       handle_audit_slop_detected)
    dispatcher.register("phase.budget_exceeded",     handle_phase_budget_exceeded)

    # Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("harness ready; entering dispatch loop")
    runner = asyncio.create_task(dispatcher.run())

    await stop_event.wait()
    log.info("shutdown signal received; stopping dispatcher")
    await dispatcher.stop()

    with suppress(asyncio.CancelledError):
        runner.cancel()
        await runner

    await router.close()
    await pool.close()
    log.info("shutdown complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
