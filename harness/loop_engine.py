"""The research-loop ENGINE — one declarative registry + one generic driver.

Before this module the loop was driven by a handful of hand-written ``_maybe_*`` functions
in the pacemaker (adjudicate, plan, the scholarship arc, the experiment-coverage driver) and
a separate ``_rearm_research_spines`` rung in the watchdog — each with its own ad-hoc dedup
scheme. That sprawl is exactly what produced the audit's "one-shot dedup deadlock" class:
re-armers were bolted on per case as each spine was found to wedge.

Here every loop-advancing step is ONE ``Transition`` row: a from-guard (an SQL predicate
re-derived from DB state each tick — state-derived, self-healing, never replaying event
history), an owner agent (its mode dial pauses the step, never destroys the work), the event
it emits, a dedup bucket, and an optional stall SLA. The generic ``drive_all`` runs them in
order. Re-arm is no longer a special rung — a transition whose precondition is still true
simply fires again next tick under its dedup bucket.

Gated by ``LOOP_ENGINE`` (default OFF), with a ``LOOP_ENGINE_SHADOW`` mirror mode that logs
what each transition WOULD emit while the legacy ``_maybe_*`` still drive — so the engine's
firing set can be compared against the incumbent against live data before cutover. Mirrors the
existing ``*_LOOP`` / ``ARIADNE_PACE`` env-gate convention.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from harness.agent_modes import get_agent_mode
from harness.loop_predicates import ACTIVE_SQL

log = logging.getLogger(__name__)

LOOP_ENGINE = os.environ.get("LOOP_ENGINE", "").lower() in {"on", "1", "true"}
LOOP_ENGINE_SHADOW = os.environ.get("LOOP_ENGINE_SHADOW", "").lower() in {"on", "1", "true"}

# These mirror the env vars the legacy drivers read (harness.ariadne_pace / harness.dispatch).
# Duplicated here — rather than imported — to keep the engine free of an import cycle with the
# pacemaker, which imports the engine. The single source of truth is the env.
EXPERIMENT_COVERAGE_TARGET = int(os.environ.get("EXPERIMENT_COVERAGE_TARGET", "3"))
EVIDENCE_RESYNTH_STEP = int(os.environ.get("SYNTHESIS_RESYNTH_STEP", os.environ.get("SYNTHESIS_MIN_EXPERIMENTS", "3")))
EVIDENCE_CAP = int(os.environ.get("ARIADNE_EVIDENCE_CAP", "9"))
REARM_GRACE_MIN = float(os.environ.get("CLOSURE_REARM_GRACE_MIN", "30"))
REARM_CAP_PER_TICK = int(os.environ.get("CLOSURE_REARM_CAP_PER_TICK", "10"))
REARM_SYNTH_MIN = int(os.environ.get("SYNTHESIS_MIN_EXPERIMENTS", "3"))


# ── the registry shape ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Transition:
    """One loop-advancing step. ``from_guard(conn) -> list[dict]`` returns the rows that NEED
    this transition right now (re-derived from DB state). For each row the driver emits
    ``emits`` (a fixed event type or a per-row callable) with ``payload(row)`` and dedup key
    ``dedup(row, day, now_ts)``, deduplicated on the events unique index. ``owner``'s mode dial
    gates the whole transition. ``pending_singleton`` skips the tick if one of ``emits`` is
    already pending (the adjudicate/plan/arc "don't stack" rule). ``stall_since_sql`` (when set
    with ``stall_sla_min``) returns ``(claim_id,)`` rows that have been eligible longer than the
    SLA → a ``loop.unclosed[stage_stalled:{stall_stage}]`` indicator (active mode only)."""

    name: str
    owner: str
    emits: str | Callable[[dict], str]
    from_guard: Callable[..., Awaitable[list[dict]]]
    payload: Callable[[dict], dict]
    dedup: Callable[[dict, str, int], str]
    target_type: str = "claim"
    target_key: str | None = "claim_id"  # row key holding target_id; None → system sentinel 0
    pending_singleton: bool = False
    stall_sla_min: int = 0
    stall_since_sql: str | None = None
    stall_stage: str = ""


# ── from-guards (faithful translations of the legacy SQL) ──────────────────────────
async def _guard_adjudicate(conn) -> list[dict]:
    n = await conn.fetchval(
        f"SELECT count(*) FROM claims c JOIN direction_scores ds ON ds.claim_id = c.id "
        f"WHERE c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
        f"AND NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id)"
    )
    return [{"claim_id": 0, "n": n}] if n else []


async def _guard_plan(conn) -> list[dict]:
    n = await conn.fetchval(
        f"SELECT count(*) FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
        f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
        f"AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id)"
    )
    return [{"claim_id": 0, "n": n}] if n else []


def _arc_guard(kind_have: str | None, kind_need: str) -> Callable[..., Awaitable[list[dict]]]:
    """Build a one-per-tick arc guard: approved + active, has ``kind_have`` (None = none required),
    lacks a final ``kind_need``. Mirrors _maybe_scholarship's review/proposal branches."""
    have_clause = (
        f"AND EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id "
        f"  AND rd.kind = '{kind_have}' AND rd.status = 'final') "
        if kind_have
        else ""
    )

    async def _guard(conn) -> list[dict]:
        cid = await conn.fetchval(
            f"SELECT c.id FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
            f"{have_clause}"
            f"AND NOT EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id "
            f"  AND rd.kind = '{kind_need}' AND rd.status = 'final') "
            f"ORDER BY c.id LIMIT 1"
        )
        return [{"claim_id": cid}] if cid is not None else []

    return _guard


async def _guard_arc_article(conn) -> list[dict]:
    # Article: a finding on file AND a settled point (concluded OR evidence-capped), no final article.
    cid = await conn.fetchval(
        f"SELECT c.id FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
        f"WHERE c.claim_kind = 'direction' AND dg.status = 'approved' "
        f"AND EXISTS (SELECT 1 FROM research_findings rf WHERE rf.direction_claim_id = c.id) "
        f"AND (c.status = 'concluded' OR (c.status IN {ACTIVE_SQL} AND "
        f"     (SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id "
        f"      WHERE t.claim_id = c.id AND e.status = 'completed') >= $1)) "
        f"AND NOT EXISTS (SELECT 1 FROM research_documents rd "
        f"  WHERE rd.claim_id = c.id AND rd.kind = 'article' AND rd.status = 'final') "
        f"ORDER BY c.id LIMIT 1",
        EVIDENCE_CAP,
    )
    return [{"claim_id": cid}] if cid is not None else []


async def _guard_experiment_coverage(conn) -> list[dict]:
    if EXPERIMENT_COVERAGE_TARGET <= 0:
        return []
    rows = await conn.fetch(
        f"""
        SELECT c.id,
               (SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                  WHERE t.claim_id = c.id AND e.status = 'completed') AS done,
               (SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                  WHERE t.claim_id = c.id AND e.status IN ('completed','failed','killed')) AS attempts,
               (SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                  WHERE t.claim_id = c.id AND e.status NOT IN ('completed','failed','killed')) AS inflight,
               (SELECT t.id FROM tasks t WHERE t.claim_id = c.id AND t.department = 'research'
                  ORDER BY t.id DESC LIMIT 1) AS task_id,
               (SELECT rf.n_experiments FROM research_findings rf
                  WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS last_synth_n
        FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id
        WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL}
          AND NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'hold')
          AND EXISTS (SELECT 1 FROM research_documents rd
                        WHERE rd.claim_id = c.id AND rd.kind = 'proposal' AND rd.status = 'final')
        """
    )
    out: list[dict] = []
    for r in rows:
        target = EXPERIMENT_COVERAGE_TARGET
        if r["last_synth_n"] is not None:
            target = min(r["last_synth_n"] + EVIDENCE_RESYNTH_STEP, EVIDENCE_CAP)
        if r["done"] >= target or r["inflight"] > 0 or r["task_id"] is None or r["attempts"] >= target * 3:
            continue
        out.append({"claim_id": r["id"], "task_id": r["task_id"], "attempts": r["attempts"]})
    return out


async def _guard_confirm_real_data(conn) -> list[dict]:
    """ESCALATION: a direction with a finding whose evidence is ALL synthetic/builtin (no real
    dataset) and nothing in flight → request ONE real-data confirmation experiment. Synthetic is
    allowed as a pilot, but a real-world claim is only settled once a REAL-data run confirms it."""
    rows = await conn.fetch(
        f"""
        SELECT c.id,
               (SELECT t.id FROM tasks t WHERE t.claim_id = c.id AND t.department = 'research'
                  ORDER BY t.id DESC LIMIT 1) AS task_id
        FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id AND dg.status = 'approved'
        WHERE c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL}
          AND EXISTS (SELECT 1 FROM research_documents rd
                        WHERE rd.claim_id = c.id AND rd.kind = 'proposal' AND rd.status = 'final')
          AND EXISTS (SELECT 1 FROM research_findings rf WHERE rf.direction_claim_id = c.id)
          AND EXISTS (SELECT 1 FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                        WHERE t.claim_id = c.id AND e.status = 'completed')
          AND NOT EXISTS (SELECT 1 FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                            WHERE t.claim_id = c.id AND e.data_realism = 'real')
          AND NOT EXISTS (SELECT 1 FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                            WHERE t.claim_id = c.id AND e.status NOT IN ('completed','failed','killed'))
          AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.status = 'pending'
                            AND ev.event_type = 'experiment.requested' AND ev.target_id = c.id)
        ORDER BY c.id LIMIT $1
        """,
        REARM_CAP_PER_TICK,
    )
    return [{"claim_id": r["id"], "task_id": r["task_id"]} for r in rows if r["task_id"] is not None]


async def _guard_rearm_interpret(conn) -> list[dict]:
    rows = await conn.fetch(
        "SELECT e.id, e.status, e.task_id, t.claim_id FROM experiment_runs e "
        "LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.status IN ('completed','failed','killed') "
        "AND COALESCE(e.interpretation, e.researcher_notes) IS NULL "
        "AND COALESCE(e.completed_at, e.killed_at) < now() - make_interval(mins => $1::int) "
        "AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.status = 'pending' "
        "  AND ev.event_type IN ('experiment.completed','experiment.failed') "
        "  AND ev.payload->>'experiment_id' = e.id::text) "
        "ORDER BY e.id LIMIT $2",
        int(REARM_GRACE_MIN),
        REARM_CAP_PER_TICK,
    )
    return [
        {"experiment_id": r["id"], "status": r["status"], "task_id": r["task_id"], "claim_id": r["claim_id"]}
        for r in rows
    ]


async def _guard_rearm_conclude(conn) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT c.id, count(e.*) AS done FROM claims c "
        f"JOIN direction_gate dg ON dg.claim_id = c.id AND dg.status = 'approved' "
        f"JOIN tasks t ON t.claim_id = c.id "
        f"JOIN experiment_runs e ON e.task_id = t.id AND e.status = 'completed' "
        f"WHERE c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
        f"AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.status = 'pending' "
        f"  AND ev.event_type = 'finding.synthesize' AND ev.target_id = c.id) "
        f"GROUP BY c.id "
        f"HAVING count(e.*) >= $1 "
        f"AND max(e.completed_at) < now() - make_interval(mins => $2::int) "
        f"AND COALESCE((SELECT max(rf.n_experiments) FROM research_findings rf "
        f"  WHERE rf.direction_claim_id = c.id), 0) < count(e.*) "
        f"LIMIT $3",
        REARM_SYNTH_MIN,
        int(REARM_GRACE_MIN),
        REARM_CAP_PER_TICK,
    )
    return [{"claim_id": r["id"], "done": r["done"]} for r in rows]


async def _guard_rearm_audit(conn) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT DISTINCT t.id FROM findings f "
        f"JOIN tasks t ON t.id = f.task_id "
        f"JOIN claims c ON c.id = f.claim_id "
        f"WHERE f.audit_verdict IS NULL AND t.status = 'completed' AND t.department = 'research' "
        f"AND c.status IN {ACTIVE_SQL} "
        f"AND t.completed_at < now() - make_interval(mins => $1::int) "
        f"AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.status = 'pending' "
        f"  AND ev.event_type = 'task.completed' AND ev.target_id = t.id) "
        f"ORDER BY t.id LIMIT $2",
        int(REARM_GRACE_MIN),
        REARM_CAP_PER_TICK,
    )
    return [{"task_id": r["id"]} for r in rows]


async def _guard_rearm_attack(conn) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT f.id, f.claim_id, f.relevance_score FROM findings f "
        f"JOIN claims c ON c.id = f.claim_id "
        f"WHERE f.audit_verdict = 'pass' AND f.relevance_score >= 8 "
        f"AND c.status IN {ACTIVE_SQL} "
        f"AND f.created_at < now() - make_interval(mins => $1::int) "
        f"AND NOT EXISTS (SELECT 1 FROM critic_verdicts cv WHERE cv.claim_id = f.claim_id "
        f"  AND cv.created_at > f.created_at) "
        f"AND NOT EXISTS (SELECT 1 FROM events ev WHERE ev.status = 'pending' "
        f"  AND ev.event_type = 'finding.high_signal' AND ev.target_id = f.claim_id) "
        f"ORDER BY f.id LIMIT $2",
        int(REARM_GRACE_MIN),
        REARM_CAP_PER_TICK,
    )
    out: list[dict] = []
    seen: set[int] = set()  # one finding per claim per tick (the critic attacks the CLAIM)
    for r in rows:
        if r["claim_id"] in seen:
            continue
        seen.add(r["claim_id"])
        out.append({"claim_id": r["claim_id"], "finding_id": r["id"], "score": float(r["relevance_score"])})
    return out


# ── the registry ────────────────────────────────────────────────────────────────
# Declared order matches the legacy pacemaker tick (adjudicate → plan → arc → coverage),
# followed by the four re-armers the watchdog used to run.
REGISTRY: list[Transition] = [
    Transition(
        name="adjudicate",
        owner="ariadne",
        emits="direction.adjudicate",
        from_guard=_guard_adjudicate,
        payload=lambda r: {},
        target_type="system",
        target_key=None,
        dedup=lambda r, day, ts: f"pace-adjudicate-{ts}",
        pending_singleton=True,
        stall_sla_min=60,
        stall_stage="adjudicate",
        stall_since_sql=(
            f"SELECT c.id FROM claims c JOIN direction_scores ds ON ds.claim_id = c.id "
            f"WHERE c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
            f"AND NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id) "
            f"AND ds.created_at < now() - make_interval(mins => $1::int)"
        ),
    ),
    Transition(
        name="plan",
        owner="ariadne",
        emits="planner.plan",
        from_guard=_guard_plan,
        payload=lambda r: {},
        target_type="system",
        target_key=None,
        dedup=lambda r, day, ts: f"pace-plan-{ts}",
        pending_singleton=True,
        stall_sla_min=60,
        stall_stage="plan",
        stall_since_sql=(
            f"SELECT c.id FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
            f"AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id) "
            f"AND dg.decided_at < now() - make_interval(mins => $1::int)"
        ),
    ),
    Transition(
        name="arc_review",
        owner="ariadne",
        emits="ariadne.review",
        from_guard=_arc_guard(None, "lit_review"),
        payload=lambda r: {"claim_id": r["claim_id"]},
        dedup=lambda r, day, ts: f"arc-ariadne.review-{r['claim_id']}-{day}",
        pending_singleton=True,
        stall_sla_min=1440,
        stall_stage="review",
        stall_since_sql=(
            f"SELECT c.id FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
            f"AND NOT EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id "
            f"  AND rd.kind = 'lit_review' AND rd.status = 'final') "
            f"AND dg.decided_at < now() - make_interval(mins => $1::int)"
        ),
    ),
    Transition(
        name="arc_propose",
        owner="ariadne",
        emits="ariadne.propose",
        from_guard=_arc_guard("lit_review", "proposal"),
        payload=lambda r: {"claim_id": r["claim_id"]},
        dedup=lambda r, day, ts: f"arc-ariadne.propose-{r['claim_id']}-{day}",
        pending_singleton=True,
        stall_sla_min=1440,
        stall_stage="proposal",
        stall_since_sql=(
            f"SELECT c.id FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {ACTIVE_SQL} "
            f"AND EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id "
            f"  AND rd.kind = 'lit_review' AND rd.status = 'final') "
            f"AND NOT EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id "
            f"  AND rd.kind = 'proposal' AND rd.status = 'final') "
            f"AND (SELECT min(rd.created_at) FROM research_documents rd WHERE rd.claim_id = c.id "
            f"  AND rd.kind = 'lit_review' AND rd.status = 'final') < now() - make_interval(mins => $1::int)"
        ),
    ),
    Transition(
        name="arc_article",
        owner="synthesis",
        emits="synthesis.article",
        from_guard=_guard_arc_article,
        payload=lambda r: {"claim_id": r["claim_id"]},
        dedup=lambda r, day, ts: f"arc-synthesis.article-{r['claim_id']}-{day}",
        pending_singleton=True,
    ),
    Transition(
        name="experiment_coverage",
        owner="experiments",
        emits="experiment.requested",
        from_guard=_guard_experiment_coverage,
        payload=lambda r: {"claim_id": r["claim_id"], "task_id": r["task_id"], "trigger": "coverage"},
        dedup=lambda r, day, ts: f"drive-exp-{r['claim_id']}-{r['attempts']}-{ts // 21600}",
    ),
    Transition(
        name="confirm_real_data",
        owner="experiments",
        emits="experiment.requested",
        from_guard=_guard_confirm_real_data,
        payload=lambda r: {
            "claim_id": r["claim_id"],
            "task_id": r["task_id"],
            "require_real_data": True,
            "trigger": "confirm_real",
        },
        dedup=lambda r, day, ts: f"confirm-real-{r['claim_id']}-{day}",
    ),
    Transition(
        name="rearm_interpret",
        owner="experiments",
        emits=lambda r: "experiment.completed" if r["status"] == "completed" else "experiment.failed",
        from_guard=_guard_rearm_interpret,
        payload=lambda r: {
            "experiment_id": r["experiment_id"],
            "claim_id": r["claim_id"],
            "task_id": r["task_id"],
            "rearmed": True,
        },
        dedup=lambda r, day, ts: f"rearm-exp-{r['experiment_id']}-{day}",
        target_type="experiment",
        target_key="experiment_id",
    ),
    Transition(
        name="rearm_conclude",
        owner="synthesis",
        emits="finding.synthesize",
        from_guard=_guard_rearm_conclude,
        payload=lambda r: {"claim_id": r["claim_id"], "experiment_count": r["done"], "rearmed": True},
        dedup=lambda r, day, ts: f"rearm-synth-{r['claim_id']}-{day}",
    ),
    Transition(
        name="rearm_audit",
        owner="evaluation",
        emits="task.completed",
        from_guard=_guard_rearm_audit,
        payload=lambda r: {"rearmed": True},
        dedup=lambda r, day, ts: f"rearm-audit-{r['task_id']}-{day}",
        target_type="task",
        target_key="task_id",
    ),
    Transition(
        name="rearm_attack",
        owner="critic",
        emits="finding.high_signal",
        from_guard=_guard_rearm_attack,
        payload=lambda r: {"finding_id": r["finding_id"], "score": r["score"], "rearmed": True},
        dedup=lambda r, day, ts: f"rearm-highsig-{r['finding_id']}-{day}",
    ),
]


# ── the generic driver ────────────────────────────────────────────────────────────
async def drive(pool, t: Transition, *, shadow: bool = False) -> list[dict]:
    """Run ONE transition: mode-gate → from_guard → (pending-singleton) → emit each row under
    its dedup bucket → stall-SLA indicator. Returns the list of fired (or would-fire, in shadow)
    ``{transition, event, target_id, dedup}`` records for logging/comparison."""
    if await get_agent_mode(pool, t.owner) not in {"advisory", "active"}:
        return []
    fired: list[dict] = []
    async with pool.acquire() as conn:
        if (
            t.pending_singleton
            and isinstance(t.emits, str)
            and await conn.fetchval("SELECT count(*) FROM events WHERE event_type = $1 AND status = 'pending'", t.emits)
        ):
            return []
        rows = await t.from_guard(conn)
        if not rows:
            await _maybe_flag_stall(conn, t, shadow=shadow)
            return []
        day = await conn.fetchval("SELECT to_char(now(), 'YYYY-MM-DD')")
        now_ts = int(time.time())
        for r in rows:
            event_type = t.emits(r) if callable(t.emits) else t.emits
            target_id = 0 if t.target_key is None else r[t.target_key]
            dedup = t.dedup(r, day, now_ts)
            rec = {"transition": t.name, "event": event_type, "target_id": target_id, "dedup": dedup}
            if shadow:
                fired.append(rec)
                continue
            res = await conn.execute(
                "INSERT INTO events (event_type, target_type, target_id, payload, status, dedup_key) "
                "VALUES ($1, $2, $3, $4::jsonb, 'pending', $5) "
                "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
                event_type,
                t.target_type,
                target_id,
                json.dumps(t.payload(r)),
                dedup,
            )
            if str(res).endswith(" 1"):
                fired.append(rec)
        await _maybe_flag_stall(conn, t, shadow=shadow)
    if fired and not shadow:
        log.info("loop_engine: %s emitted %d event(s)", t.name, len(fired))
    return fired


async def _maybe_flag_stall(conn, t: Transition, *, shadow: bool) -> None:
    """A direction eligible for this transition longer than its SLA → loop.unclosed
    [stage_stalled:{stage}] (the handler is dead/suppressed, or a precondition never clears).
    Sentinel target_id 0 + day-bucketed key, mirroring _flag_agenda_exhausted. Active mode
    only — in shadow the legacy drivers still run, so a stall would be a false alarm."""
    if shadow or not t.stall_sla_min or not t.stall_since_sql:
        return
    day = await conn.fetchval("SELECT to_char(now(), 'YYYY-MM-DD')")
    stalled = await conn.fetch(t.stall_since_sql, t.stall_sla_min)
    for r in stalled:
        await conn.execute(
            "INSERT INTO events (event_type, target_type, target_id, payload, dedup_key) "
            "VALUES ('loop.unclosed', 'system', 0, $1::jsonb, $2) "
            "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
            json.dumps({"kind": f"stage_stalled:{t.stall_stage}", "claim_id": r["id"], "sla_min": t.stall_sla_min}),
            f"stage-stalled-{t.stall_stage}-{r['id']}-{day}",
        )


async def drive_all(pool, *, shadow: bool = False) -> list[dict]:
    """Run every transition in declared order. Returns all fired (or would-fire) records."""
    out: list[dict] = []
    for t in REGISTRY:
        try:
            out.extend(await drive(pool, t, shadow=shadow))
        except Exception:  # noqa: BLE001 — one bad transition must not stop the rest (or kill the pacemaker)
            log.exception("loop_engine: transition %s failed", t.name)
    if shadow and out:
        summary = ", ".join(f"{r['transition']}:{r['target_id']}" for r in out)
        log.info("loop_engine[SHADOW]: would emit %d event(s) — %s", len(out), summary)
    return out
