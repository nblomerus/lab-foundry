"""
Trace view — sessions and their step DAGs.

Each handler invocation creates an `agent_sessions` row; every model call
inside it lands in `agent_runs` with a `session_id` / `step_name` /
`parent_step_id` / `step_order`. This router exposes:

    GET  /trace/sessions               list with filters
    GET  /trace/sessions/{id}          one session + its full step graph
    GET  /trace/graph/claim/{id}       claim's evidence chain + verdicts
    GET  /trace/graph/stats            node and edge counts in Neo4j

The /trace web page subscribes to `/ws/events` and patches the DAG live as
`step.started` / `step.completed` / `step.failed` events arrive.
Neo4j graph endpoints return query results for visualization.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)


router = APIRouter(prefix="/trace", tags=["trace"])


def _latency_ms(started, completed) -> int | None:
    if started and completed:
        return int((completed - started).total_seconds() * 1000)
    return None


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = 50,
    handler_name: str | None = None,
    status: str | None = None,
    mode: str | None = None,
) -> dict:
    """Recent sessions, newest first. Filterable by handler, status, mode."""
    pool = request.app.state.pool
    where, args = [], []
    if handler_name:
        args.append(handler_name)
        where.append(f"s.handler_name = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"s.status = ${len(args)}")
    if mode:
        args.append(mode)
        where.append(f"s.mode = ${len(args)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(min(limit, 200))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                s.id, s.handler_name, s.status, s.mode,
                s.started_at, s.completed_at, s.error,
                s.triggered_by_event_id,
                e.event_type AS trigger_event_type,
                e.target_type AS trigger_target_type,
                e.target_id   AS trigger_target_id,
                COUNT(r.id) FILTER (WHERE r.session_id = s.id) AS step_count,
                COUNT(r.id) FILTER (WHERE r.session_id = s.id AND r.status = 'failed') AS failed_steps,
                COALESCE(SUM(r.input_token_count), 0)  AS input_tokens,
                COALESCE(SUM(r.output_token_count), 0) AS output_tokens
            FROM agent_sessions s
            LEFT JOIN events e ON e.id = s.triggered_by_event_id
            LEFT JOIN agent_runs r ON r.session_id = s.id
            {clause}
            GROUP BY s.id, e.event_type, e.target_type, e.target_id
            ORDER BY s.id DESC LIMIT ${len(args)}
            """,
            *args,
        )
        # Facets for filter UI (24h window so they reflect what's actually live).
        handler_rows = await conn.fetch(
            "SELECT handler_name, COUNT(*) AS n FROM agent_sessions "
            "WHERE started_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY handler_name ORDER BY n DESC"
        )
        status_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM agent_sessions "
            "WHERE started_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY status ORDER BY n DESC"
        )

    sessions = [
        {
            "id": r["id"],
            "handler_name": r["handler_name"],
            "status": r["status"],
            "mode": r["mode"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "latency_ms": _latency_ms(r["started_at"], r["completed_at"]),
            "error": r["error"],
            "trigger_event_id": r["triggered_by_event_id"],
            "trigger_event_type": r["trigger_event_type"],
            "trigger_target_type": r["trigger_target_type"],
            "trigger_target_id": r["trigger_target_id"],
            "step_count": r["step_count"],
            "failed_steps": r["failed_steps"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        for r in rows
    ]

    return {
        "sessions": sessions,
        "facets": {
            "handlers": {r["handler_name"]: r["n"] for r in handler_rows},
            "statuses": {r["status"]: r["n"] for r in status_rows},
        },
    }


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: int) -> dict:
    """Full session + every linked agent_run, ordered by step_order.

    The frontend builds the DAG from these rows: nodes are runs, edges are
    parent_step_id → child runs. step.* events arriving on /ws/events for
    this session_id patch nodes in place.
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            """
            SELECT
                s.id, s.handler_name, s.status, s.mode,
                s.started_at, s.completed_at, s.error,
                s.triggered_by_event_id,
                e.event_type AS trigger_event_type,
                e.target_type AS trigger_target_type,
                e.target_id   AS trigger_target_id,
                e.payload     AS trigger_payload
            FROM agent_sessions s
            LEFT JOIN events e ON e.id = s.triggered_by_event_id
            WHERE s.id = $1
            """,
            session_id,
        )
        if sess is None:
            return {"session": None, "runs": []}

        runs = await conn.fetch(
            """
            SELECT
                id, invocation_type, model_tier, model_name, status,
                started_at, completed_at,
                input_token_count, output_token_count,
                input_summary, output_summary, error,
                step_name, parent_step_id, step_order,
                fallback_attempts, langfuse_trace_id
            FROM agent_runs
            WHERE session_id = $1
            ORDER BY step_order NULLS LAST, id
            """,
            session_id,
        )

    session = {
        "id": sess["id"],
        "handler_name": sess["handler_name"],
        "status": sess["status"],
        "mode": sess["mode"],
        "started_at": sess["started_at"].isoformat() if sess["started_at"] else None,
        "completed_at": sess["completed_at"].isoformat() if sess["completed_at"] else None,
        "latency_ms": _latency_ms(sess["started_at"], sess["completed_at"]),
        "error": sess["error"],
        "trigger_event_id": sess["triggered_by_event_id"],
        "trigger_event_type": sess["trigger_event_type"],
        "trigger_target_type": sess["trigger_target_type"],
        "trigger_target_id": sess["trigger_target_id"],
        "trigger_payload": dict(sess["trigger_payload"]) if sess["trigger_payload"] else None,
    }

    runs_out = [
        {
            "id": r["id"],
            "invocation_type": r["invocation_type"],
            "model_tier": r["model_tier"],
            "model_name": r["model_name"],
            "status": r["status"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "latency_ms": _latency_ms(r["started_at"], r["completed_at"]),
            "input_tokens": r["input_token_count"],
            "output_tokens": r["output_token_count"],
            "input_summary": r["input_summary"],
            "output_summary": r["output_summary"],
            "error": r["error"],
            "step_name": r["step_name"],
            "parent_step_id": r["parent_step_id"],
            "step_order": r["step_order"],
            "fallback_attempts": list(r["fallback_attempts"]) if r["fallback_attempts"] else [],
            "langfuse_trace_id": r["langfuse_trace_id"],
        }
        for r in runs
    ]

    return {"session": session, "runs": runs_out}


# =========================================================================
# Neo4j Graph Visualization Endpoints
# =========================================================================


@router.get("/graph/stats")
async def graph_stats(request: Request) -> dict:
    """Get Neo4j graph node and edge counts."""
    try:
        from labfoundry.mcp_servers.labfoundry_knowledge.tools import _get_driver

        driver = await _get_driver()
        async with driver.session() as session:
            claims = await session.run("MATCH (c:Claim) RETURN COUNT(c) AS count")
            findings = await session.run("MATCH (f:Finding) RETURN COUNT(f) AS count")
            verdicts = await session.run("MATCH (v:CriticVerdict) RETURN COUNT(v) AS count")
            grounds_edges = await session.run("MATCH (f:Finding)-[:GROUNDS]->(c:Claim) RETURN COUNT(*) AS count")
            challenged_edges = await session.run(
                "MATCH (v:CriticVerdict)-[:CHALLENGED]->(c:Claim) RETURN COUNT(*) AS count"
            )
            cited_edges = await session.run("MATCH (f:Finding)-[:CITED_BY]->(v:CriticVerdict) RETURN COUNT(*) AS count")

            return {
                "status": "ok",
                "nodes": {
                    "claims": (await claims.data())[0]["count"],
                    "findings": (await findings.data())[0]["count"],
                    "verdicts": (await verdicts.data())[0]["count"],
                },
                "edges": {
                    "grounds": (await grounds_edges.data())[0]["count"],
                    "challenged": (await challenged_edges.data())[0]["count"],
                    "cited_by": (await cited_edges.data())[0]["count"],
                },
            }
    except Exception as e:
        log.warning("graph_stats failed: %s", e)
        return {"status": "unavailable", "error": str(e)}


@router.get("/graph/claim/{claim_id}")
async def graph_claim(request: Request, claim_id: int) -> dict:
    """Get a claim with its evidence chain and critic verdicts."""
    try:
        from labfoundry.mcp_servers.labfoundry_knowledge.tools import (
            get_claim_critics,
            get_claim_evidence_chain,
        )

        evidence = await get_claim_evidence_chain(claim_id, limit=50)
        critics = await get_claim_critics(claim_id)

        return {
            "status": "ok",
            "claim_id": claim_id,
            "evidence_chain": evidence,
            "critic_verdicts": critics,
        }
    except Exception as e:
        log.warning("graph_claim failed for claim %d: %s", claim_id, e)
        return {"status": "unavailable", "claim_id": claim_id, "error": str(e)}
