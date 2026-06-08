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
    min_steps: int = 0,
) -> dict:
    """Recent sessions, newest first. Filterable by handler, status, mode.

    `min_steps` (default 0) hides sessions with fewer than N model-call steps —
    most sessions are deterministic handlers (Mimir ingest) with 0 LLM steps, so
    `min_steps=1` surfaces only the ones with an actual DAG to inspect.
    """
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

    having = ""
    if min_steps > 0:
        args.append(min_steps)
        having = f"HAVING COUNT(r.id) FILTER (WHERE r.session_id = s.id) >= ${len(args)}"

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
            {having}
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
        from library.graph.tools import _get_driver

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
        from library.graph.tools import (
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


def _compact(payload) -> dict | None:
    """Trim an event payload for transport — keep the shape, cap long strings."""
    if not isinstance(payload, dict):
        return None
    out: dict = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + "…"
        elif isinstance(v, dict):
            out[k] = {kk: (vv[:200] + "…" if isinstance(vv, str) and len(vv) > 200 else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out or None


def _outcome(steps: list[dict], has_doc: bool, queryable: bool) -> tuple[str, str]:
    """Reduce a journey's steps to a single (outcome, reason) verdict."""
    kinds = {s["kind"] for s in steps}
    if queryable and "ingest" in kinds:
        return "ingested", "in the Library, retrievable"
    if "rejected" in kinds:
        rej = next((s for s in steps if s["kind"] == "rejected"), None)
        return "rejected", (rej or {}).get("detail", "rejected before ingest")
    if "blocked" in kinds:
        blk = next((s for s in steps if s["kind"] == "blocked"), None)
        return "blocked", (blk or {}).get("detail", "blocked by Mimir")
    if has_doc:
        return "in_library", "document created"
    return "pending", "discovered, not yet resolved"


@router.get("/journeys")
async def journeys(
    request: Request,
    limit: int = 60,
    outcome: str | None = None,
    kind: str | None = None,
    q: str | None = None,
) -> dict:
    """Browse recent INTERACTIONS without needing an id — one row per source, newest
    first, each with its start/end and outcome (ingested / rejected / blocked / pending).
    Open one to see the full event chain. Filter by `outcome`, source `kind`, or search `q`."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH recent AS (
                SELECT payload->'source'->>'canonical_key' AS canonical_key,
                       payload->'source'->>'kind'          AS source_kind,
                       payload->'source'->>'title'         AS title,
                       id AS event_id, emitted_at
                FROM events
                WHERE event_type = 'source.discovered'
                  AND payload->'source'->>'canonical_key' IS NOT NULL
                ORDER BY emitted_at DESC
                LIMIT 2000
            ), dedup AS (
                SELECT DISTINCT ON (canonical_key) *
                FROM recent ORDER BY canonical_key, emitted_at DESC
            )
            SELECT d.canonical_key, d.source_kind, d.title, d.event_id, d.emitted_at AS started_at,
                   doc.id AS doc_id, doc.status AS doc_status, doc.queryable, doc.trust_tier,
                   doc.title AS doc_title, doc.ingested_at
            FROM dedup d
            LEFT JOIN documents doc ON doc.canonical_key = d.canonical_key
            ORDER BY d.emitted_at DESC
            LIMIT 600
            """
        )
        keys = [r["canonical_key"] for r in rows]
        # Latest rejection per key, in one pass.
        rej = {}
        if keys:
            for rr in await conn.fetch(
                """
                SELECT DISTINCT ON (payload->>'canonical_key')
                       payload->>'canonical_key' AS ck, payload->>'reason' AS reason,
                       payload->>'stage' AS stage, emitted_at
                FROM events
                WHERE event_type = 'library.ingest_rejected'
                  AND payload->>'canonical_key' = ANY($1)
                ORDER BY payload->>'canonical_key', emitted_at DESC
                """,
                keys,
            ):
                rej[rr["ck"]] = rr
        # Which of these documents were blocked by Mimir (a doc row exists but never went queryable).
        blocked: set[int] = set()
        susp = [r["doc_id"] for r in rows if r["doc_id"] is not None and not r["queryable"]]
        if susp:
            for br in await conn.fetch(
                "SELECT DISTINCT target_id FROM events WHERE event_type = 'mimir.ingest_blocked' AND target_id = ANY($1)",
                susp,
            ):
                blocked.add(br["target_id"])

    items = []
    for r in rows:
        ck = r["canonical_key"]
        has_doc = r["doc_id"] is not None
        if has_doc and r["queryable"]:
            oc, reason = "ingested", f"{r['trust_tier']} · in the Library"
        elif ck in rej:
            oc, reason = "rejected", rej[ck]["reason"] or "rejected before ingest"
        elif has_doc and r["doc_id"] in blocked:
            oc, reason = "blocked", "blocked by Mimir before ingest"
        elif has_doc:
            oc, reason = "in_library", f"{r['doc_status']}"
        else:
            oc, reason = "pending", "discovered, not yet resolved"
        items.append(
            {
                "canonical_key": ck,
                "source_kind": r["source_kind"],
                "title": r["doc_title"] or r["title"] or ck,
                "doc_id": r["doc_id"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": (r["ingested_at"] or rej.get(ck, {}).get("emitted_at"))
                and (r["ingested_at"] or rej[ck]["emitted_at"]).isoformat(),
                "outcome": oc,
                "outcome_reason": reason,
            }
        )

    if outcome:
        items = [i for i in items if i["outcome"] == outcome]
    if kind:
        items = [i for i in items if i["source_kind"] == kind]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["title"] or "").lower() or ql in (i["canonical_key"] or "").lower()]

    facets: dict[str, int] = {}
    for i in items:
        facets[i["outcome"]] = facets.get(i["outcome"], 0) + 1
    return {"journeys": items[: min(limit, 200)], "facets": facets, "total": len(items)}


@router.get("/journey/{ref:path}")
async def journey(request: Request, ref: str) -> dict:
    """The FULL trace of one interaction — every event from the first sighting to the
    terminal outcome, with each event's payload (input) and result (output). Assembled
    from domain keys (the deterministic path writes no agent_runs), keyed on the
    canonical_key so it spans the source AND its document — including sources that were
    REJECTED or BLOCKED and never became a document. `ref` = document id, canonical_key,
    or arxiv id."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # Resolve ref → (canonical_key, document row|None).
        d = None
        if ref.isdigit():
            d = await conn.fetchrow(
                "SELECT id, title, source_kind, canonical_key, trust_tier, trust_state, status, "
                "queryable, ingested_at FROM documents WHERE id = $1",
                int(ref),
            )
        if d is None:
            d = await conn.fetchrow(
                "SELECT id, title, source_kind, canonical_key, trust_tier, trust_state, status, "
                "queryable, ingested_at FROM documents WHERE canonical_key = $1 OR arxiv_id = $1 "
                "ORDER BY ingested_at DESC NULLS LAST LIMIT 1",
                ref,
            )
        canonical_key = d["canonical_key"] if d else ref
        source_kind = d["source_kind"] if d else None

        # If there's no document, learn the source from its discovery event.
        disc_meta = None
        if d is None:
            disc_meta = await conn.fetchrow(
                "SELECT payload->'source'->>'kind' AS kind, payload->'source'->>'title' AS title, "
                "payload->'source'->>'url' AS url FROM events WHERE event_type = 'source.discovered' "
                "AND payload->'source'->>'canonical_key' = $1 ORDER BY emitted_at DESC LIMIT 1",
                canonical_key,
            )
            if disc_meta:
                source_kind = disc_meta["kind"]

        steps: list[dict] = []

        if source_kind:
            seen = await conn.fetchrow(
                "SELECT first_seen_at, attempts FROM discovery_seen WHERE source_kind = $1 AND canonical_key = $2",
                source_kind,
                canonical_key,
            )
            if seen:
                steps.append(
                    {
                        "at": seen["first_seen_at"],
                        "kind": "scout",
                        "label": "Scout sighting",
                        "detail": f"{source_kind} scout surfaced it (attempts={seen['attempts']})",
                        "status": None,
                        "event_id": None,
                        "session_id": None,
                        "payload": None,
                    }
                )

        for e in await conn.fetch(
            "SELECT id, emitted_at, status, payload, session_id FROM events "
            "WHERE event_type = 'source.discovered' AND payload->'source'->>'canonical_key' = $1 "
            "ORDER BY emitted_at LIMIT 5",
            canonical_key,
        ):
            src = (e["payload"] or {}).get("source", {}) if isinstance(e["payload"], dict) else {}
            steps.append(
                {
                    "at": e["emitted_at"],
                    "kind": "discovered",
                    "label": "source.discovered",
                    "detail": src.get("why") or src.get("url") or "",
                    "status": e["status"],
                    "event_id": e["id"],
                    "session_id": e["session_id"],
                    "payload": _compact(e["payload"]),
                }
            )

        for e in await conn.fetch(
            "SELECT id, emitted_at, status, payload FROM events WHERE event_type = 'library.ingest_rejected' "
            "AND payload->>'canonical_key' = $1 ORDER BY emitted_at",
            canonical_key,
        ):
            p = e["payload"] if isinstance(e["payload"], dict) else {}
            steps.append(
                {
                    "at": e["emitted_at"],
                    "kind": "rejected",
                    "label": "library.ingest_rejected",
                    "detail": f"{p.get('reason', 'rejected')} (stage: {p.get('stage', '?')})",
                    "status": e["status"],
                    "event_id": e["id"],
                    "session_id": None,
                    "payload": _compact(e["payload"]),
                }
            )

        if d is not None:
            for c in await conn.fetch(
                "SELECT id, decision, to_tier, used_llm, reasons, decided_by_run_id, created_at "
                "FROM certifications WHERE document_id = $1 ORDER BY created_at",
                d["id"],
            ):
                session_id = None
                if c["decided_by_run_id"]:
                    r = await conn.fetchrow("SELECT session_id FROM agent_runs WHERE id = $1", c["decided_by_run_id"])
                    session_id = r["session_id"] if r else None
                blocked = c["decision"] != "approve"
                steps.append(
                    {
                        "at": c["created_at"],
                        "kind": "blocked" if blocked else "certify",
                        "label": "Mimir " + ("blocked" if blocked else "certified"),
                        "detail": f"→ {c['to_tier']} · used_llm={c['used_llm']} · {(c['reasons'] or '')[:140]}",
                        "status": c["decision"],
                        "event_id": None,
                        "session_id": session_id,
                        "payload": {"to_tier": c["to_tier"], "used_llm": c["used_llm"], "reasons": c["reasons"]},
                    }
                )
            for ev in await conn.fetch(
                "SELECT id, event_type, emitted_at, status, payload, session_id FROM events "
                "WHERE target_type = 'document' AND target_id = $1 ORDER BY emitted_at",
                d["id"],
            ):
                kind = {"document.parsed": "parse", "document.ingested": "ingest", "mimir.ingest_blocked": "blocked"}.get(
                    ev["event_type"], "event"
                )
                p = ev["payload"] if isinstance(ev["payload"], dict) else {}
                detail = ""
                if kind == "ingest":
                    detail = (
                        f"{p.get('n_chunks', '?')} chunks · {p.get('embedded', '?')} embedded · {p.get('trust_tier', '')}"
                    )
                elif ev["event_type"] == "mimir.ingest_blocked":
                    detail = (p.get("reasons") or "")[:140]
                steps.append(
                    {
                        "at": ev["emitted_at"],
                        "kind": kind,
                        "label": ev["event_type"],
                        "detail": detail,
                        "status": ev["status"],
                        "event_id": ev["id"],
                        "session_id": ev["session_id"],
                        "payload": _compact(ev["payload"]),
                    }
                )

        steps.sort(key=lambda s: (s["at"] is None, s["at"]))
        outcome, reason = _outcome(steps, d is not None, bool(d and d["queryable"]))

        title = (d["title"] if d else None) or (disc_meta["title"] if disc_meta else None) or canonical_key
        return {
            "subject": {
                "canonical_key": canonical_key,
                "source_kind": source_kind,
                "title": title,
                "doc_id": d["id"] if d else None,
                "trust_tier": d["trust_tier"] if d else None,
                "trust_state": d["trust_state"] if d else None,
                "status": d["status"] if d else None,
                "queryable": bool(d["queryable"]) if d else False,
                "ingested_at": d["ingested_at"].isoformat() if d and d["ingested_at"] else None,
                "outcome": outcome,
                "outcome_reason": reason,
            },
            "steps": [{**s, "at": s["at"].isoformat() if s["at"] else None} for s in steps],
        }
