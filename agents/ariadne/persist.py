"""
Persist Ariadne's direction tree — the advisory/active write path.

In SHADOW she writes nothing (ops.ariadne_firstlight). In ADVISORY/ACTIVE her output
becomes the agenda: a mission claim → direction claims (claim_kind, on claims.parent_id —
no new tree table, per the locked design) → per-direction claim_goals. Everything lands
as `status='proposed'` (the human/Planner review queue); advisory just means nothing
downstream acts until a human flips ariadne to active. History is preserved — each
deliberation writes a fresh mission+directions (the PI re-frames each round).
"""

from __future__ import annotations

import json
import logging

from agents.ariadne.scoring import DIMENSIONS, composite, is_wellformed, priority_label
from agents.mimir.acquire import AcquireRequest as MimirAcquireRequest
from agents.mimir.acquire import request_acquire

log = logging.getLogger(__name__)


async def request_evidence(state, requests) -> int:
    """Fire Ariadne's evidence requests into Mimir's acquire QUEUE — the demand side of the
    Library. Each AcquireRequest targets a SPECIFIC paper (an arxiv id when she knows it, else
    its exact title) she believes is missing — NOT a broad topic (the scouts already cover
    topics, so a topic query just returns already_have). Mimir adjudicates (cap → resolve →
    dedupe → trust-gated ingest). Best effort — a bad request must not fail the deliberation.
    Returns the count emitted."""
    n = 0
    for r in requests or []:
        why = r.why if len(r.why or "") >= 30 else f"{r.why or r.paper} — Ariadne wants this specific paper"
        try:
            # Prefer the exact arxiv id (a precise fetch); fall back to the exact title as a query.
            mreq = (
                MimirAcquireRequest(requester="pi", kind="paper", arxiv_id=r.arxiv_id, why=why)
                if r.arxiv_id
                else MimirAcquireRequest(requester="pi", kind="paper", query=r.paper, why=why)
            )
            await request_acquire(state, mreq)
            n += 1
        except Exception as e:  # noqa: BLE001 — demand side is best-effort
            log.warning("ariadne: acquire request failed for %r: %s", r.paper, e)
    if n:
        log.info("ariadne: queued %d specific-paper request(s) to Mimir", n)
    return n


async def persist_directions(state, out, *, run_id: int | None = None) -> dict:
    """Write mission → directions → claim_goals + decision scores (all 'proposed'). Returns counts.

    Supersedes the PRIOR active agenda first (one active mission at a time) so re-framings
    don't pile up across the continuous loop — old rows are invalidated, not deleted, so
    history is preserved and the move stays reversible."""
    async with state.pool.acquire() as conn, conn.transaction():
        superseded = await conn.execute(
            "UPDATE claims SET status='invalidated', invalidated_at=now(), "
            "invalidation_reason='superseded by a newer deliberation' "
            "WHERE claim_kind IN ('mission','direction') "
            "AND status IN ('proposed','tested','weakly_supported','replicated')"
        )
        mission_id = await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status, confidence, created_by_run_id) "
            "VALUES ($1, 'mission', 'proposed', 0.5, $2) RETURNING id",
            out.mission_frame[:4000],
            run_id,
        )
        n_dir = n_goal = n_scored = 0
        for d in out.directions:
            dir_id = await conn.fetchval(
                "INSERT INTO claims (statement, claim_kind, parent_id, status, confidence, created_by_run_id) "
                "VALUES ($1, 'direction', $2, 'proposed', 0.5, $3) RETURNING id",
                f"{d.title}: {d.statement}"[:4000],
                mission_id,
                run_id,
            )
            n_dir += 1
            if is_wellformed(d.scores):
                comp = composite(d.scores)
                await conn.execute(
                    "INSERT INTO direction_scores "
                    "(claim_id, novelty, feasibility, evidence_availability, paper_potential, reviewer_interest, "
                    " technical_depth, differentiation, cost_efficiency, lab_alignment, composite, priority, rationale) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                    dir_id,
                    *[getattr(d.scores, dim) for dim in DIMENSIONS],
                    comp,
                    priority_label(comp),
                    d.scores.rationale,
                )
                n_scored += 1
            for g in d.claim_goals:
                await conn.execute(
                    "INSERT INTO claim_goals "
                    "(claim_id, expectation, kill_condition, novelty_target, "
                    "next_milestone, priority_hint, status, set_by_run_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'open', $7)",
                    dir_id,
                    g.expectation,
                    g.kill_condition,
                    g.novelty_target,
                    g.next_milestone,
                    g.priority_hint,
                    run_id,
                )
                n_goal += 1
    n_superseded = int(superseded.split()[-1]) if superseded and superseded.startswith("UPDATE") else 0
    log.info(
        "ariadne: superseded %d prior claim(s); persisted mission %s + %d directions + %d goals + %d scored",
        n_superseded,
        mission_id,
        n_dir,
        n_goal,
        n_scored,
    )
    return {
        "mission_id": mission_id,
        "directions": n_dir,
        "claim_goals": n_goal,
        "scored": n_scored,
        "superseded": n_superseded,
    }


async def persist_reflection(state, out, valid_ids, *, run_id: int | None = None) -> dict:
    """Apply a reflection: steer the standing directions + record strategic lessons.

    All writes are reversible (status flips, priority overrides, probationary lessons) and
    confined to Ariadne's precious-small tier:
      * retire       → claims.status='invalidated' (+ invalidation_reason)
      * reprioritize/pivot → direction_scores.priority := new_priority (scored directions only)
      * advance      → affirmation, no data change
      * lessons      → probationary rows in `lessons` (derived_via='reflection'), fed back
                       into future deliberation via recall_lessons.
    """
    vids = set(valid_ids)
    counts = {"retired": 0, "reprioritized": 0, "advanced": 0, "lessons": 0}
    async with state.pool.acquire() as conn, conn.transaction():
        for v in out.verdicts:
            if v.claim_id not in vids:
                continue
            if v.assessment == "retire":
                await conn.execute(
                    "UPDATE claims SET status='invalidated', invalidated_at=now(), invalidation_reason=$2 "
                    "WHERE id=$1 AND claim_kind='direction'",
                    v.claim_id,
                    v.reason[:2000],
                )
                counts["retired"] += 1
            elif v.assessment in ("reprioritize", "pivot") and v.new_priority:
                await conn.execute(
                    "UPDATE direction_scores SET priority=$2 WHERE claim_id=$1", v.claim_id, v.new_priority
                )
                counts["reprioritized"] += 1
            elif v.assessment == "advance":
                counts["advanced"] += 1
        for les in out.lessons:
            if not les.lesson.strip():
                continue
            await conn.execute(
                "INSERT INTO lessons (applies_to_invocation, applies_when, lesson_text, rationale, "
                "derived_from_run_id, derived_via, status, confidence) "
                "VALUES ('ariadne.deliberate', $1, $2, $3, $4, 'reflection', 'probationary', 0.40)",
                json.dumps({"when": les.applies_when} if les.applies_when else {}),
                les.lesson[:2000],
                (les.rationale or "")[:2000],
                run_id,
            )
            counts["lessons"] += 1
    log.info("ariadne: reflection applied — %s", counts)
    return counts
