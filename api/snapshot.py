"""
LabFoundry REST snapshot endpoints. The frontend fetches these on page load and on
reconnect; WebSocket pushes deltas in between.
Exposes the lab's research state: claims, evidence, tasks, phase, and agent activity.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
from fastapi import APIRouter, Request

from api.models import (
    AgentRunOut,
    CompanyStateOut,
    CostTrackingOut,
    DissentItem,
    EdgeActivity,
    EventOut,
    FindingOut,
    OrgRoleOut,
    PhaseTransitionOut,
    SnapshotOut,
    StatsOut,
    TaskCount,
    TelemetryDay,
    ThesisOut,
)

router = APIRouter()


# Map invocation_type prefix → org role
ROLE_OF = {
    "pi": "pi",
    "planner": "planner",
    "researcher": "researcher",
    "knowledge_scout": "knowledge_scout",
    "evaluation": "evaluation",
    "critic": "critic",
    "phase_adjudicator": "phase_adjudicator",
    "reflect": "reflection",
    "curator": "curator",
}

ALL_ROLES = [
    "pi",
    "planner",
    "knowledge_scout",
    "evaluation",
    "critic",
    "researcher",
    "phase_adjudicator",
    "reflection",
    "curator",
]


async def _state(pool: asyncpg.Pool) -> CompanyStateOut:
    async with pool.acquire() as c:
        s = await c.fetchrow("SELECT * FROM company_state WHERE id = 1")
        if s is None:
            raise RuntimeError("company_state not seeded")
        active = await c.fetchval(
            "SELECT COUNT(*) FROM claims WHERE status IN ('proposed', 'tested', 'weakly_supported', 'replicated')"
        )
        killed = await c.fetchval("SELECT COUNT(*) FROM claims WHERE status IN ('invalidated', 'merged')")

    now = datetime.now(UTC)
    return CompanyStateOut(
        current_phase=s["current_phase"],
        phase_started_at=s["phase_started_at"],
        bootstrap_at=s["bootstrap_at"],
        days_in_phase=(now - s["phase_started_at"]).days,
        days_since_start=(now - s["bootstrap_at"]).days,
        problem_statement=s["problem_statement"],
        stance=s["stance"],
        success_criterion=s["success_criterion"],
        thesis=s["thesis"],
        niche=s["niche"],
        audience=s["audience"],
        charter=s["charter"],
        paused=s["paused"],
        paused_reason=s["paused_reason"],
        active_claims_count=active or 0,
        invalidated_claims_count=killed or 0,
    )


async def _theses_with_counts(pool: asyncpg.Pool, status_filter: str, limit: int) -> list[ThesisOut]:
    # Filter for active (not invalidated/merged) or inactive claims
    if status_filter == "active":
        status_clause = "status IN ('proposed', 'tested', 'weakly_supported', 'replicated')"
    else:
        status_clause = "status IN ('invalidated', 'merged')"

    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            SELECT t.*,
              (SELECT COUNT(*) FROM findings f WHERE f.claim_id = t.id
                AND COALESCE(f.audit_verdict,'') != 'stale') AS finding_count,
              (SELECT COUNT(*) FROM findings f WHERE f.claim_id = t.id
                AND f.supports_thesis = true AND f.audit_verdict = 'pass') AS supporting_count,
              (SELECT COUNT(*) FROM findings f WHERE f.claim_id = t.id
                AND f.supports_thesis = false AND f.audit_verdict = 'pass') AS contradicting_count
            FROM claims t
            WHERE {status_clause}
            ORDER BY confidence DESC NULLS LAST, updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        ThesisOut(
            id=r["id"],
            claim=r["statement"],
            status=r["status"],
            confidence=float(r["confidence"]),
            confidence_prev=float(r["confidence_prev"]) if r["confidence_prev"] is not None else None,
            parent_id=r["parent_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            invalidated_at=r["invalidated_at"],
            kill_reason=r["invalidation_reason"],
            finding_count=r["finding_count"] or 0,
            supporting_count=r["supporting_count"] or 0,
            contradicting_count=r["contradicting_count"] or 0,
        )
        for r in rows
    ]


async def _findings(pool: asyncpg.Pool, limit: int) -> list[FindingOut]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM findings WHERE COALESCE(audit_verdict, '') != 'stale' ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return [
        FindingOut(
            id=r["id"],
            task_id=r["task_id"],
            claim_id=r["claim_id"],
            source=r["source"],
            url=r["url"],
            title=r["title"],
            summary=r["summary"],
            relevance_score=float(r["relevance_score"]),
            why_it_matters=r["why_it_matters"],
            audit_score=float(r["audit_score"]) if r["audit_score"] is not None else None,
            audit_verdict=r["audit_verdict"],
            supports_thesis=r["supports_thesis"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def _runs(pool: asyncpg.Pool, limit: int) -> list[AgentRunOut]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT $1",
            limit,
        )
    return [
        AgentRunOut(
            id=r["id"],
            department=r["department"],
            invocation_type=r["invocation_type"],
            model_tier=r["model_tier"],
            model_name=r["model_name"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            status=r["status"],
            input_token_count=r["input_token_count"],
            output_token_count=r["output_token_count"],
            output_summary=r["output_summary"],
            error=r["error"],
            langfuse_trace_id=r["langfuse_trace_id"],
        )
        for r in rows
    ]


async def _dissent(pool: asyncpg.Pool, limit: int) -> list[DissentItem]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT 'critic' AS kind, av.id, av.thesis_id AS claim_id,
                   av.verdict AS detail, av.confidence,
                   av.reasoning, av.created_at
            FROM critic_verdicts av
            UNION ALL
            SELECT 'audit-slop' AS kind, f.id, f.claim_id,
                   f.audit_verdict AS detail,
                   f.audit_score AS confidence,
                   f.summary AS reasoning, f.created_at
            FROM findings f WHERE f.audit_verdict = 'slop'
            ORDER BY created_at DESC LIMIT $1
            """,
            limit,
        )
    return [
        DissentItem(
            kind=r["kind"],
            id=r["id"],
            claim_id=r["claim_id"],
            detail=r["detail"] or "?",
            confidence=float(r["confidence"]) if r["confidence"] is not None else None,
            reasoning=r["reasoning"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def _phase_transitions(pool: asyncpg.Pool, limit: int) -> list[PhaseTransitionOut]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM phase_transitions ORDER BY decided_at DESC LIMIT $1",
            limit,
        )
    return [
        PhaseTransitionOut(
            id=r["id"],
            from_phase=r["from_phase"],
            to_phase=r["to_phase"],
            reason=r["reason"],
            forced=r["forced"],
            decided_at=r["decided_at"],
        )
        for r in rows
    ]


async def _cost(pool: asyncpg.Pool) -> CostTrackingOut:
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM cost_tracking WHERE day = CURRENT_DATE")
    if row is None:
        return CostTrackingOut(
            day=None,
            reasoning_calls=0,
            workhorse_calls=0,
            fast_calls=0,
            code_calls=0,
            total_cost_usd=0.0,
            cap_reached=False,
        )
    return CostTrackingOut(
        day=row["day"].isoformat(),
        reasoning_calls=row["reasoning_calls"],
        workhorse_calls=row["workhorse_calls"],
        fast_calls=row["fast_calls"],
        code_calls=row["code_calls"],
        total_cost_usd=float(row["total_cost_usd"]),
        cap_reached=row["cap_reached"],
    )


async def _org(pool: asyncpg.Pool) -> list[OrgRoleOut]:
    """Build org-chart roles from agent_runs aggregates."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT
              department AS role,
              COUNT(*) FILTER (WHERE status = 'running')                       AS running_count,
              MAX(started_at)                                                  AS last_run_at,
              COUNT(*) FILTER (WHERE started_at > NOW() - INTERVAL '24 hours') AS runs_today,
              AVG(EXTRACT(EPOCH FROM (completed_at - started_at)))
                  FILTER (WHERE completed_at IS NOT NULL
                          AND started_at > NOW() - INTERVAL '24 hours')        AS avg_duration_s
            FROM agent_runs
            GROUP BY department
            """
        )
    by_role = {r["role"]: r for r in rows}
    out: list[OrgRoleOut] = []
    for role in ALL_ROLES:
        r = by_role.get(role)
        out.append(
            OrgRoleOut(
                role=role,
                running_count=int(r["running_count"]) if r else 0,
                last_run_at=r["last_run_at"] if r else None,
                runs_today=int(r["runs_today"]) if r else 0,
                avg_duration_s=float(r["avg_duration_s"]) if r and r["avg_duration_s"] else None,
            )
        )
    return out


async def _lesson_counts(pool: asyncpg.Pool) -> dict[str, int]:
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT status, COUNT(*) AS n FROM lessons GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


async def _telemetry(pool: asyncpg.Pool) -> list[TelemetryDay]:
    """Last 7 days of runs, findings, and tokens."""
    async with pool.acquire() as c:
        runs = await c.fetch(
            """
            SELECT
              date_trunc('day', started_at)::date AS day,
              COUNT(*) AS runs,
              COALESCE(SUM(input_token_count + output_token_count), 0) AS tokens
            FROM agent_runs
            WHERE started_at > NOW() - INTERVAL '7 days'
            GROUP BY day
            """
        )
        findings = await c.fetch(
            """
            SELECT
              date_trunc('day', created_at)::date AS day,
              COUNT(*) AS findings
            FROM findings
            WHERE created_at > NOW() - INTERVAL '7 days'
              AND COALESCE(audit_verdict, '') != 'stale'
            GROUP BY day
            """
        )

    from datetime import date, timedelta

    runs_by_day = {r["day"]: r for r in runs}
    findings_by_day = {f["day"]: f["findings"] for f in findings}

    out: list[TelemetryDay] = []
    today = date.today()
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        r = runs_by_day.get(d)
        out.append(
            TelemetryDay(
                day=d.isoformat(),
                label=labels[d.weekday()],
                runs=int(r["runs"]) if r else 0,
                findings=int(findings_by_day.get(d, 0)),
                tokens=int((r["tokens"] or 0) // 1000) if r else 0,
            )
        )
    return out


async def _task_counts(pool: asyncpg.Pool) -> list[TaskCount]:
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    by_status = {r["status"]: r["n"] for r in rows}
    return [
        TaskCount(label=s, value=by_status.get(s, 0)) for s in ("pending", "running", "completed", "failed", "halted")
    ]


async def _stats(pool: asyncpg.Pool) -> StatsOut:
    # "today" = last 24h, not the calendar day. Calendar day flips at midnight
    # UTC and instantly hides everything from the last few hours, which is not
    # what a dashboard user wants.
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM tasks WHERE status = 'pending')                       AS pending_tasks,
              (SELECT COUNT(*) FROM tasks WHERE status = 'running')                       AS running_tasks,
              (SELECT COUNT(*) FROM findings
                 WHERE created_at > NOW() - INTERVAL '24 hours'
                   AND COALESCE(audit_verdict,'') != 'stale')                             AS findings_today,
              (SELECT COUNT(*) FROM findings
                 WHERE created_at > NOW() - INTERVAL '24 hours'
                   AND audit_verdict = 'pass' AND relevance_score >= 8)                   AS high_signal_today,
              (SELECT COUNT(*) FROM findings
                 WHERE created_at > NOW() - INTERVAL '24 hours'
                   AND audit_verdict = 'slop')                                            AS slop_today,
              (SELECT COUNT(*) FROM agent_runs
                 WHERE status = 'failed'
                   AND started_at > NOW() - INTERVAL '24 hours'
                   -- Orphans (process killed mid-run) aren't genuine failures;
                   -- the reaper tags them so they don't cry wolf on the dash.
                   AND COALESCE(error, '') NOT LIKE '%orphan reaped%')                    AS failed_runs_today,
              (SELECT COUNT(*) FROM events
                 WHERE status = 'failed'
                   AND emitted_at > NOW() - INTERVAL '24 hours')                          AS schema_failures_today,
              (SELECT COUNT(*) FROM tasks
                 WHERE status = 'running' AND department = 'research'
                   AND payload->'sources' ? 'hacker_news')                                AS source_hn_in_flight,
              (SELECT COUNT(*) FROM tasks
                 WHERE status = 'running' AND department = 'research'
                   AND payload->'sources' ? 'reddit')                                     AS source_reddit_in_flight,
              (SELECT COUNT(*) FROM tasks
                 WHERE status = 'running' AND department = 'research'
                   AND payload->'sources' ? 'web')                                        AS source_web_in_flight,
              -- Newest heartbeat across the system. Drives the dashboard's
              -- live/quiet/stalled indicator. GREATEST ignores NULLs.
              GREATEST(
                (SELECT MAX(started_at)  FROM agent_runs),
                (SELECT MAX(emitted_at)  FROM events),
                (SELECT MAX(created_at)  FROM findings)
              )                                                                           AS last_activity_at
            """
        )
    return StatsOut(**dict(row))


async def _edge_activity(pool: asyncpg.Pool) -> list[EdgeActivity]:
    """Aggregate events of interest for the live-flow page."""
    interesting = [
        "task.created",
        "task.completed",
        "finding.high_signal",
        "thesis.invalidated",
        "thesis.created",
        "thesis.confidence_changed",
        "phase.transition_proposed",
        "phase.budget_exceeded",
        "queue.empty",
        "audit.slop_detected",
    ]
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT
              event_type,
              COUNT(*) FILTER (WHERE emitted_at > NOW() - INTERVAL '1 minute')  AS count_last_minute,
              COUNT(*) FILTER (WHERE emitted_at > NOW() - INTERVAL '24 hours') AS count_today,
              MAX(emitted_at)                                                  AS last_fired_at
            FROM events
            WHERE event_type = ANY($1::text[])
            GROUP BY event_type
            """,
            interesting,
        )
    by_type = {r["event_type"]: r for r in rows}
    out: list[EdgeActivity] = []
    for et in interesting:
        r = by_type.get(et)
        out.append(
            EdgeActivity(
                event_type=et,
                count_last_minute=int(r["count_last_minute"]) if r else 0,
                count_today=int(r["count_today"]) if r else 0,
                last_fired_at=r["last_fired_at"] if r else None,
            )
        )
    return out


# =========================================================================
# Routes
# =========================================================================


@router.get("/snapshot", response_model=SnapshotOut)
async def snapshot(request: Request) -> SnapshotOut:
    import os

    pool: asyncpg.Pool = request.app.state.pool
    (
        state,
        active,
        killed,
        findings,
        runs,
        dissent,
        phases,
        org,
        cost,
        lessons,
        telemetry,
        task_counts,
        stats,
        edge_activity,
    ) = await asyncio.gather(
        _state(pool),
        _theses_with_counts(pool, "active", 20),
        _theses_with_counts(pool, "killed", 10),
        _findings(pool, 30),
        _runs(pool, 25),
        _dissent(pool, 20),
        _phase_transitions(pool, 10),
        _org(pool),
        _cost(pool),
        _lesson_counts(pool),
        _telemetry(pool),
        _task_counts(pool),
        _stats(pool),
        _edge_activity(pool),
    )
    # Surface langfuse host so the dashboard can link directly to traces.
    lf_host = os.environ.get("LANGFUSE_HOST") if os.environ.get("LANGFUSE_PUBLIC_KEY") else None
    return SnapshotOut(
        state=state,
        active_claims=active,
        invalidated_claims=killed,
        recent_findings=findings,
        recent_runs=runs,
        dissent=dissent,
        phase_transitions=phases,
        org_roles=org,
        cost=cost,
        lesson_counts=lessons,
        telemetry=telemetry,
        task_counts=task_counts,
        stats=stats,
        edge_activity=edge_activity,
        langfuse_host=lf_host,
    )


@router.get("/events", response_model=list[EventOut])
async def events(request: Request, limit: int = 100) -> list[EventOut]:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM events ORDER BY emitted_at DESC LIMIT $1",
            limit,
        )
    return [
        EventOut(
            id=r["id"],
            event_type=r["event_type"],
            target_type=r["target_type"],
            target_id=r["target_id"],
            payload=r["payload"] if isinstance(r["payload"], dict) else {},
            status=r["status"],
            suppression_reason=r["suppression_reason"],
            emitted_at=r["emitted_at"],
            consumed_at=r["consumed_at"],
            consumed_by_handler=r["consumed_by_handler"],
        )
        for r in rows
    ]


@router.get("/theses/{claim_id}/findings", response_model=list[FindingOut])
async def thesis_findings(claim_id: int, request: Request) -> list[FindingOut]:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM findings WHERE claim_id = $1 "
            "AND COALESCE(audit_verdict,'') != 'stale' "
            "ORDER BY created_at DESC",
            claim_id,
        )
    return [
        FindingOut(
            id=r["id"],
            task_id=r["task_id"],
            claim_id=r["claim_id"],
            source=r["source"],
            url=r["url"],
            title=r["title"],
            summary=r["summary"],
            relevance_score=float(r["relevance_score"]),
            why_it_matters=r["why_it_matters"],
            audit_score=float(r["audit_score"]) if r["audit_score"] is not None else None,
            audit_verdict=r["audit_verdict"],
            supports_thesis=r["supports_thesis"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
