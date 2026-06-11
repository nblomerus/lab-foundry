"""
Ariadne's event-driven handler — runs her in the live loop, gated by the mode dial.

Triggered by `ariadne.deliberate`. The dispatcher only invokes this when ariadne's mode
is advisory|active (off|shadow are paused there — shadow deliberation is the read-only
ops.ariadne_firstlight). So reaching here means: deliberate over the trusted substrate,
then PERSIST the direction tree (advisory: proposed, awaiting human review before active
agents act on it). Being in agents.ariadne.* means `agent_of` maps it to 'ariadne', so
the same mode dial that pauses any agent pauses Ariadne.
"""

from __future__ import annotations

import logging

from agents.ariadne.grade import grade, grade_reflection
from agents.ariadne.loop import run_shadow
from agents.ariadne.persist import persist_directions, persist_reflection, request_evidence
from agents.ariadne.reflect import run_reflection
from agents.llm import current_run_id
from harness.agent_modes import get_agent_mode

log = logging.getLogger(__name__)


async def handle_ariadne_deliberate(event: dict, dispatcher) -> dict | None:
    """Deliberate + persist the direction tree (advisory/active). Read-only if shadow."""
    state = dispatcher.state
    mode = await get_agent_mode(state.pool, "ariadne")

    focus = (event.get("payload") or {}).get("focus")  # an injected debug request narrows the topic
    # Live path: emit the Ariadne↔Mimir conversation so the floorplan shows it (the corpus is
    # still untouched — only telemetry events are written; persist happens below for adv/active).
    out = await run_shadow(state, focus=focus, emit_conversation=True)
    report = await grade(out)
    summary = {
        "mode": mode,
        "directions": len(out.directions),
        "graded_pass": report.passed,
        "citations_resolved": round(report.citations_resolved, 2),
    }

    # shadow should not reach here (dispatcher pauses it) — defend anyway: write nothing.
    if mode not in {"advisory", "active"}:
        log.info("ariadne: mode=%s — deliberated, wrote nothing", mode)
        return {**summary, "persisted": False}

    # Advisory gate: only persist a graded-passing tree (no hallucinated-citation agendas).
    if not report.passed:
        log.warning(
            "ariadne: deliberation FAILED grading (citations=%.0f%%) — not persisting", report.citations_resolved * 100
        )
        return {**summary, "persisted": False, "reason": "failed_grading"}

    counts = await persist_directions(state, out)
    n_requests = await request_evidence(state, out.requests)  # demand side → Mimir acquire queue
    log.info("ariadne: %s — persisted %s, queued %d evidence requests", mode, counts, n_requests)
    return {**summary, "persisted": True, **counts, "evidence_requests": n_requests}


async def handle_ariadne_reflect(event: dict, dispatcher) -> dict | None:
    """REFLECT & STEER the standing agenda (advisory/active). Read-only if shadow."""
    state = dispatcher.state
    mode = await get_agent_mode(state.pool, "ariadne")

    # Live path: emit the Ariadne↔Mimir reflection conversation (floorplan + history); corpus untouched.
    out, valid_ids = await run_reflection(state, emit_conversation=True)
    if out is None:
        log.info("ariadne reflect: no standing directions to steer")
        return {"mode": mode, "reflected": False, "reason": "empty_agenda"}

    report = grade_reflection(out, valid_ids)
    summary = {"mode": mode, "verdicts": report.n_verdicts, "lessons": report.n_lessons, "graded_pass": report.passed}

    if mode not in {"advisory", "active"}:
        log.info("ariadne reflect: mode=%s — reflected, wrote nothing", mode)
        return {**summary, "persisted": False}
    if not report.passed:
        log.warning("ariadne reflect: FAILED grading (invalid refs=%s) — not persisting", report.invalid_refs)
        return {**summary, "persisted": False, "reason": "failed_grading"}

    # The reflect run is the last _chain_complete on this session — credit re-derived lessons to it.
    counts = await persist_reflection(state, out, valid_ids, run_id=current_run_id())
    log.info("ariadne reflect: %s — applied %s", mode, counts)
    return {**summary, "persisted": True, **counts}
