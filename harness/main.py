"""
LabFoundry harness entry point — the autonomous research lab's event-driven agent loop.

Run after bootstrap:

    python -m harness.main

Wires Postgres pool, state/memory/lessons clients, Curator, Router,
Dispatcher; registers handlers for the research lifecycle (knowledge acquisition,
evaluation, critic, planning, reflection); runs forever until SIGINT/SIGTERM.

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

# Stage 0 (market-PI neutralized): the legacy market-lifecycle PI handlers
# (claim_invalidated / phase_adjudicator / phase_budget_exceeded / phase_transition)
# are intentionally NOT imported or registered — see the registration block below.
from agents.ariadne.handler import handle_ariadne_deliberate, handle_ariadne_reflect
from agents.critic.handler import handle_finding_high_signal
from agents.evaluation.handler import handle_task_completed
from agents.evaluation.slop_handler import handle_audit_slop_detected
from agents.experiments.handler import (
    handle_experiment_completed,
    handle_experiment_failed,
    handle_experiment_requested,
)
from agents.planner.decompose import handle_planner_decompose
from agents.planner.handler import handle_queue_empty
from agents.reflection.handler import handle_reflection_requested
from agents.researcher.grounded_handler import handle_grounded_research
from harness.curator import Curator
from harness.dispatch import Dispatcher
from harness.router import GPULock, Router, build_cloud_chain, build_premium_chain
from library.graph.sink import handle_graph_sink_claim_created
from memory.client import ZepClient
from skills.client import LessonsClient
from state.client import PostgresClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("harness")


# Sessions Zep must contain (created lazily on first use, but eager is cleaner)
ZEP_SESSIONS = [
    "claims-lifecycle",
    "phase-transitions",
    "pi-deliberations",
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

    # Neo4j — non-fatal. The graph is a read-optimized projection; its absence
    # degrades query quality but never blocks the research loop. We bootstrap
    # BOTH the cognition constraints (Claim/Finding/CriticVerdict) and the
    # corpus constraints (Paper/Dataset/Source/Author) the Librarian MERGEs into;
    # a missing/unavailable Neo4j driver is swallowed exactly like the others.
    try:
        from library.graph.tools import (
            ensure_constraints,
            ensure_corpus_constraints,
        )

        await ensure_constraints()
        await ensure_corpus_constraints()
        log.info("preflight: neo4j OK")
    except Exception as e:
        log.error("preflight: NEO4J DEGRADED — graph writes will fail silently: %s", e)

    # Embed model — non-fatal. The corpus read path (labfoundry_corpus) embeds
    # queries via Ollama; if the model isn't pulled, retrieval degrades to empty
    # results rather than blocking the loop. Guard both endpoint shapes: modern
    # POST /api/embed {model,input} and legacy POST /api/embeddings {model,prompt}.
    embed_model = os.environ.get("EMBED_MODEL", "nomic-embed-text")  # 768-dim default
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{ollama_url}/api/embed",
                json={"model": embed_model, "input": "preflight"},
            )
            if r.status_code == 404:
                # Older Ollama: fall back to the legacy endpoint shape.
                r = await c.post(
                    f"{ollama_url}/api/embeddings",
                    json={"model": embed_model, "prompt": "preflight"},
                )
            r.raise_for_status()
        log.info("preflight: embed OK (%s)", embed_model)
    except Exception as e:
        log.error("preflight: EMBED DEGRADED — corpus query embedding will fail (model=%s): %s", embed_model, e)

    return ok


async def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    max_concurrent = int(os.environ.get("MAX_CONCURRENT_HANDLERS", "4"))
    gpu_call_timeout = float(os.environ.get("GPU_CALL_TIMEOUT_S", "300"))
    cloud_chain = build_cloud_chain(os.environ)
    premium_chain = build_premium_chain(os.environ)

    log.info("starting harness")
    log.info("  db=%s", db_url)
    log.info("  ollama=%s", ollama_url)
    log.info("  max_concurrent_handlers=%d", max_concurrent)
    log.info("  gpu_call_timeout_s=%.0f", gpu_call_timeout)
    if premium_chain:
        log.info(
            "  premium (reasoning): %s → free chain",
            " → ".join(f"{cp.provider.value}({cp.model_name})" for cp in premium_chain),
        )
    if cloud_chain:
        log.info("  free chain: %s → local", " → ".join(f"{cp.provider.value}({cp.model_name})" for cp in cloud_chain))
    else:
        log.info("  cloud chain: (none — all-local)")

    async def _register_vector_codec(conn):
        # Phase-2 corpus writes bind list[float] as vector(768)
        # (state.set_chunk_embeddings); asyncpg needs the pgvector codec registered
        # per-connection. Best-effort: if the pgvector pkg or the `vector` extension
        # is absent, the corpus embed path degrades but the rest of the harness is
        # unaffected (mirrors labfoundry_corpus._init_conn).
        try:
            import pgvector.asyncpg

            await pgvector.asyncpg.register_vector(conn)
        except Exception as e:
            log.warning(
                "pgvector codec not registered on harness pool (corpus embed writes will fail until fixed): %s", e
            )

    pool = await asyncpg.create_pool(db_url, min_size=4, max_size=20, init=_register_vector_codec)

    # Bootstrap check
    async with pool.acquire() as conn:
        bootstrapped = await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1")
    if not bootstrapped:
        log.error("company_state not seeded; run `python -m ops.bootstrap` first")
        await pool.close()
        return 1

    # Build clients and components
    state = PostgresClient(pool=pool)
    memory = ZepClient.from_env()
    lessons = LessonsClient(pool=pool)

    # Preflight: fail loud now if a hard dependency is broken, rather than
    # degrading silently mid-run (the Zep memory->thread break failed 82
    # events before anyone noticed). DB + Ollama are fatal; Zep is non-fatal
    # (memory is a nice-to-have) but a broken API shape is logged loudly.
    if not await _preflight(pool, ollama_url, memory):
        await pool.close()
        return 1

    # Sequence Zep init calls with a small gap so the boot burst is shaped
    # rather than fire-everything-at-once. Free-tier /threads caps at 5/min,
    # which 6 sequenced calls still exceed — but doing them here (a) makes the
    # cap-hit deterministic and visible in preflight instead of intermittent
    # in handler execution, and (b) gives a single tunable knob if you upgrade
    # the plan or want to add longer backoff later. Combined with single-flight
    # in ensure_session and the defensive write_message, a 429 here is logged
    # and the rest of boot continues.
    zep_gap_s = float(os.environ.get("ZEP_INIT_GAP_S", "0.25"))
    await memory.ensure_user()
    for session in ZEP_SESSIONS:
        await asyncio.sleep(zep_gap_s)
        await memory.ensure_session(session)

    curator = Curator(state=state, memory=memory, lessons=lessons)
    gpu_lock = GPULock()
    router = Router(
        pool=pool,
        gpu_lock=gpu_lock,
        ollama_url=ollama_url,
        call_timeout_s=gpu_call_timeout,
        cloud_chain=cloud_chain,
        premium_chain=premium_chain,
    )
    dispatcher = Dispatcher(pool=pool, max_concurrent_handlers=max_concurrent)

    # Attach clients so handlers can reach them via the dispatcher param
    dispatcher.state = state
    dispatcher.memory = memory
    dispatcher.lessons = lessons
    dispatcher.curator = curator
    dispatcher.router = router

    # KNOWLEDGE_CORE_ONLY: run just Mimir + the collectors (the Library's intake
    # side), leaving the research workflow dormant. The research handlers fire
    # only on their trigger events (task.created etc.), so NOT registering them
    # means a bootstrapped agenda's tasks sit idle and only Mimir acts — the
    # cleanest "first living agent" start. The claim.created KG sink stays (it
    # just projects seeded claims into Neo4j; harmless + useful in both modes).
    knowledge_core_only = os.environ.get("KNOWLEDGE_CORE_ONLY", "").lower() in {"1", "true", "on", "yes"}

    # Register handlers — covers the full frame → submit loop
    if not knowledge_core_only:
        dispatcher.register("task.completed", handle_task_completed)
        dispatcher.register("finding.high_signal", handle_finding_high_signal)
        dispatcher.register("queue.empty", handle_queue_empty)
        dispatcher.register("reflection.requested", handle_reflection_requested)
        dispatcher.register("audit.slop_detected", handle_audit_slop_detected)
        # Stage 0 — market-lifecycle PI handlers DISABLED (research-PI and market-PI
        # cannot coexist on this schema). NOT registered: claim.invalidated,
        # claim.confidence_changed (phase_adjudicator), phase.transition_proposed,
        # phase.budget_exceeded — they formed an autonomous chain that wrote a market
        # charter into company_state and swapped every agent's prompt. The trigger
        # (dispatch._check_phase_budget) and the charter injection (curator
        # ._constitution_layer) are disabled too — defence in depth.
    else:
        log.info("KNOWLEDGE_CORE_ONLY — research-workflow handlers NOT registered (Mimir + collectors only)")
    dispatcher.register("claim.created", handle_graph_sink_claim_created)
    # Ariadne (research PI) — registered ALWAYS; the per-agent mode dial gates her
    # (defaults 'off' under KNOWLEDGE_CORE_ONLY). Flip to advisory/active with ops.agent_mode.
    dispatcher.register("ariadne.deliberate", handle_ariadne_deliberate)
    dispatcher.register("ariadne.reflect", handle_ariadne_reflect)
    # Planner (Stage 2) — registered ALWAYS; the 'planner' mode dial gates it (default off).
    dispatcher.register("planner.plan", handle_planner_decompose)
    # Researcher (Stage 3, research-era Library-grounded) — registered ALWAYS; the 'researcher'
    # mode dial gates it (off/shadow = no-op, advisory/active = executes tasks → findings →
    # confidence feedback). Supersedes the market-era web researcher.
    dispatcher.register("task.created", handle_grounded_research)
    # Experiments agent (Stage 4) — registered ALWAYS; the 'experiments' mode dial gates it.
    # `experiment.requested` (from the researcher's needs_experiment) designs + queues a code
    # experiment; the Quartermaster runs the design→run→debug loop; `experiment.completed`/`.failed`
    # interpret the result into confidence feedback + a first-party Library note.
    dispatcher.register("experiment.requested", handle_experiment_requested)
    dispatcher.register("experiment.completed", handle_experiment_completed)
    dispatcher.register("experiment.failed", handle_experiment_failed)

    # Mimir — Warden of the Library. ONE agent owns ingest + trust: on a
    # discovered source it stages, classify_trust-gates, then finalizes or
    # quarantines inline (no separate Librarian agent, no ingest_approved
    # handshake). Gated on MIMIR_LOOP (env, default OFF), mirroring the other
    # *_LOOP gates.
    if os.environ.get("MIMIR_LOOP", "").lower() in {"v1", "on"}:
        from agents.mimir.handler import (
            handle_source_discovered as handle_mimir_source_discovered,
        )
        from agents.mimir.handler import (
            handle_sweep_requested as handle_mimir_sweep_requested,
        )

        dispatcher.register("source.discovered", handle_mimir_source_discovered)
        dispatcher.register("library.sweep_requested", handle_mimir_sweep_requested)

        # Acquire/pull path: PI/Researcher/Novelty ask Mimir for a specific source.
        from agents.mimir.acquire import handle_acquire_requested as handle_mimir_acquire_requested

        dispatcher.register("acquire.requested", handle_mimir_acquire_requested)
        log.info("mimir ingest loop ENABLED (MIMIR_LOOP)")

    # Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Ariadne pacemaker — condition-driven trigger for her continuous loop. Gated on
    # ARIADNE_PACE (default OFF); only fires when her mode dial is advisory|active.
    pace_task = None
    if os.environ.get("ARIADNE_PACE", "").lower() in {"on", "1", "true"}:
        from harness.ariadne_pace import ariadne_pacemaker

        pace_task = asyncio.create_task(ariadne_pacemaker(pool, stop_event))
        log.info("ariadne pacemaker ENABLED (ARIADNE_PACE)")

    # Quartermaster — resource manager + experiment execution pool (its own tasks, off the
    # dispatcher slots). Gated on QUARTERMASTER (default OFF); only acts when its mode dial is
    # advisory|active. Gets the router/curator so the experiment design→run→debug loop can call the LLM.
    qm_task = None
    if os.environ.get("QUARTERMASTER", "").lower() in {"on", "1", "true"}:
        from harness.quartermaster import quartermaster_watchdog

        qm_task = asyncio.create_task(quartermaster_watchdog(pool, stop_event, router=router, curator=curator))
        log.info("quartermaster ENABLED (QUARTERMASTER)")

    log.info("harness ready; entering dispatch loop")
    runner = asyncio.create_task(dispatcher.run())

    await stop_event.wait()
    log.info("shutdown signal received; stopping dispatcher")
    await dispatcher.stop()

    with suppress(asyncio.CancelledError):
        runner.cancel()
        await runner
        if pace_task is not None:
            pace_task.cancel()
            await pace_task
        if qm_task is not None:
            qm_task.cancel()
            await qm_task

    await router.close()
    await pool.close()
    log.info("shutdown complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
