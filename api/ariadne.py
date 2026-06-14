"""
Ariadne dashboard API — "Ariadne at a glance" + the field-model landscape.

Read-only views over what her three capability stages produce: the mission frame, the
ranked & scored direction tree (decision framework), the strategic lessons (reflect-loop),
and the field model (the Domain-Expert landscape with trend states). Powers /ariadne.
"""

from __future__ import annotations

import json
import logging
import os
import re

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agents.ariadne.scoring import DIMENSIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ariadne", tags=["ariadne"])

_ACTIVE = "('proposed','tested','weakly_supported','replicated')"
_LESSON_SCOPE = "('ariadne.deliberate','ariadne.reflect')"
# Priority gate: at most this many directions may be 'approved' (active research) at once.
GATE_BUDGET = int(os.environ.get("ARIADNE_GATE_BUDGET", "3"))
_GATE_DECISIONS = ("approved", "held", "rejected", "pending")


@router.get("/overview")
async def overview(request: Request) -> dict:
    """At-a-glance counts + the current mission, its ranked scored directions, and lessons."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mode = await conn.fetchval("SELECT mode FROM agent_modes WHERE agent_name = 'ariadne'") or "off"
        mission = await conn.fetchrow(
            "SELECT id, statement, status, created_at FROM claims WHERE claim_kind = 'mission' ORDER BY id DESC LIMIT 1"
        )

        directions = []
        if mission:
            rows = await conn.fetch(
                f"""
                SELECT c.id, c.statement, c.status, c.confidence, c.created_at, c.invalidation_reason,
                       ds.composite, ds.priority, ds.rationale,
                       {", ".join("ds." + d for d in DIMENSIONS)},
                       COALESCE(dg.status, 'pending') AS gate,
                       (SELECT count(*) FROM claim_goals g WHERE g.claim_id = c.id) AS n_goals
                FROM claims c
                LEFT JOIN direction_scores ds ON ds.claim_id = c.id
                LEFT JOIN direction_gate dg ON dg.claim_id = c.id
                WHERE c.claim_kind = 'direction' AND c.parent_id = $1
                ORDER BY COALESCE(ds.composite, 0) DESC, c.id
                """,
                mission["id"],
            )
            for r in rows:
                title, _, stmt = (r["statement"] or "").partition(": ")
                scores = {d: r[d] for d in DIMENSIONS} if r["composite"] is not None else None
                directions.append(
                    {
                        "id": r["id"],
                        "title": title or (r["statement"] or "")[:80],
                        "statement": stmt or "",
                        "status": r["status"],
                        "retired": r["status"] == "invalidated",
                        "invalidation_reason": r["invalidation_reason"],
                        "composite": float(r["composite"]) if r["composite"] is not None else None,
                        "priority": r["priority"],
                        "rationale": r["rationale"],
                        "scores": scores,
                        "n_goals": r["n_goals"],
                        "gate": r["gate"],
                    }
                )

        lessons = [
            {
                "lesson": r["lesson_text"],
                "status": r["status"],
                "when": (r["applies_when"] or {}).get("when") if isinstance(r["applies_when"], dict) else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in await conn.fetch(
                f"SELECT lesson_text, applies_when, status, created_at FROM lessons "
                f"WHERE applies_to_invocation IN {_LESSON_SCOPE} ORDER BY created_at DESC LIMIT 20"
            )
        ]

        # Scope the at-a-glance to the CURRENT mission's tree, so counts match the
        # displayed directions as deliberation history accumulates across missions.
        n_active = sum(1 for d in directions if not d["retired"])
        n_retired = sum(1 for d in directions if d["retired"])
        n_goals = sum(d["n_goals"] for d in directions)
        n_lessons = len(lessons)
        # Live counts for the floorplan (Claim Ledger + Request Queue nodes/KPIs).
        claims_total = await conn.fetchval("SELECT count(*) FROM claims")
        acquire_24h = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type = 'acquire.requested' "
            "AND emitted_at > now() - interval '24 hours'"
        )
        # Live queue DEPTH (pending), distinct from the 24h volume above — this is the
        # real "is Mimir backed up?" signal (bounded by acquire backpressure).
        acquire_pending = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type = 'acquire.requested' AND status = 'pending'"
        )
        # Execution-agent modes + research-task counts (floorplan Planner / Researchers nodes).
        modes = {
            r["agent_name"]: r["mode"]
            for r in await conn.fetch(
                "SELECT agent_name, mode FROM agent_modes "
                "WHERE agent_name IN ('planner','researcher','experiments','quartermaster',"
                "'critic','evaluation','synthesis','novelty')"
            )
        }
        critic_verdicts = await conn.fetchval("SELECT count(*) FROM critic_verdicts")
        # Total reflects the LIVE agenda — exclude tasks belonging to retired (invalidated)
        # directions; those are dead history (a few are kept only for experiment/finding lineage).
        research_tasks = await conn.fetchval(
            "SELECT count(*) FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id "
            "WHERE t.department = 'research' AND (c.status IS NULL OR c.status <> 'invalidated')"
        )
        research_pending = await conn.fetchval(
            "SELECT count(*) FROM tasks WHERE department = 'research' AND status = 'pending'"
        )
        research_running = await conn.fetchval(
            "SELECT count(*) FROM tasks WHERE department = 'research' AND status = 'running'"
        )
        exp_running = await conn.fetchval("SELECT count(*) FROM experiment_runs WHERE status IN ('running','queued')")
        exp_total = await conn.fetchval("SELECT count(*) FROM experiment_runs WHERE code IS NOT NULL")
        # Convergence: directions the lab has CONCLUDED (a decisive finding → terminal result) + the
        # total paper-shaped findings established. These accumulate permanently across re-frames.
        concluded_directions = await conn.fetchval(
            "SELECT count(*) FROM claims WHERE claim_kind = 'direction' AND status = 'concluded'"
        )
        findings_total = await conn.fetchval("SELECT count(*) FROM research_findings")
        focus = [
            r["concept_name"]
            for r in await conn.fetch(
                "SELECT concept_name FROM field_model WHERE trend_state IN ('hot','emerging') "
                "ORDER BY (trend_state = 'hot') DESC, total_papers DESC LIMIT 5"
            )
        ]
        # Is Ariadne THINKING? Her LLM runs are stamped on completion (no in-flight row), so derive a
        # recency signal: active = a deliberate/reflect/review/propose run finished in the last ~2 min;
        # stale = nothing in 6h while she's dialed on (the pacemaker expects a beat within REFLECT_MAX_AGE).
        think = await conn.fetchrow(
            "SELECT max(completed_at) AS last_at, "
            "(array_agg(invocation_type ORDER BY completed_at DESC))[1] AS last_kind, "
            "count(*) FILTER (WHERE completed_at > now() - interval '24 hours') AS runs_24h, "
            "bool_or(completed_at > now() - interval '120 seconds') AS recent, "
            "(max(completed_at) < now() - interval '6 hours') AS stale "
            "FROM agent_runs WHERE agent_name = 'ariadne'"
        )

    scored = [d for d in directions if d["composite"] is not None and not d["retired"]]
    top_priority = scored[0]["title"] if scored else next((d["title"] for d in directions if not d["retired"]), None)
    if not mission:
        status = "Dormant — no agenda framed"
    elif n_lessons:
        status = "Steering Research"
    else:
        status = "Framing Directions"

    return {
        "mode": mode,
        "at_a_glance": {
            "active_directions": n_active,
            "retired_directions": n_retired,
            "claim_goals": n_goals,
            "lessons": n_lessons,
            "top_priority": top_priority,
            "focus": focus,
            "status": status,
            # Only count LIVE approved directions — a retired/invalidated direction keeps its old
            # 'approved' gate row, which otherwise inflates the KPI (e.g. "1/6 approved" while 0 are active).
            "approved": sum(1 for d in directions if d["gate"] == "approved" and not d["retired"]),
            "gate_budget": GATE_BUDGET,
            "claims_total": claims_total,
            "acquire_requests_24h": acquire_24h,
            "acquire_pending": acquire_pending,
            "planner_mode": modes.get("planner", "off"),
            "researcher_mode": modes.get("researcher", "off"),
            "research_tasks": research_tasks,
            "research_tasks_pending": research_pending,
            "research_tasks_running": research_running,
            "experiments_mode": modes.get("experiments", "off"),
            "quartermaster_mode": modes.get("quartermaster", "off"),
            "critic_mode": modes.get("critic", "off"),
            "evaluation_mode": modes.get("evaluation", "off"),
            "synthesis_mode": modes.get("synthesis", "off"),
            "novelty_mode": modes.get("novelty", "off"),
            "critic_verdicts": critic_verdicts,
            "experiments_running": exp_running,
            "experiments_total": exp_total,
            "concluded_directions": concluded_directions,
            "findings": findings_total,
            "thinking": {
                "active": bool(think["recent"]) if think else False,
                "last_run_at": think["last_at"].isoformat() if think and think["last_at"] else None,
                "last_kind": think["last_kind"] if think else None,
                "runs_24h": (think["runs_24h"] or 0) if think else 0,
                "stalled": (bool(think["stale"]) and mode in ("advisory", "active")) if think else False,
            },
        },
        "mission": {
            "id": mission["id"],
            "statement": mission["statement"],
            "framed_at": mission["created_at"].isoformat(),
        }
        if mission
        else None,
        "directions": directions,
        "lessons": lessons,
    }


@router.get("/field-model")
async def field_model(request: Request, per_state: int = 12) -> dict:
    """The landscape grouped by trend state, plus the cohort windows and counts."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT concept_kind, concept_name, total_papers, recent_papers, prior_papers, velocity, "
            "trend_state, recent_window, prior_window FROM field_model "
            "WHERE trend_state IN ('hot','emerging','saturated','declining') "
            "ORDER BY trend_state, CASE WHEN trend_state = 'emerging' THEN recent_papers ELSE total_papers END DESC"
        )
        counts = {
            r["trend_state"]: r["n"]
            for r in await conn.fetch("SELECT trend_state, count(*) AS n FROM field_model GROUP BY trend_state")
        }

    by_state: dict[str, list] = {"hot": [], "emerging": [], "saturated": [], "declining": []}
    windows = {"recent": None, "prior": None}
    for r in rows:
        if windows["recent"] is None:
            windows = {"recent": r["recent_window"], "prior": r["prior_window"]}
        b = r["trend_state"]
        if len(by_state[b]) < per_state:
            by_state[b].append(
                {
                    "kind": r["concept_kind"],
                    "name": r["concept_name"],
                    "total": r["total_papers"],
                    "recent": r["recent_papers"],
                    "prior": r["prior_papers"],
                    "velocity": float(r["velocity"]),
                }
            )
    return {"windows": windows, "counts": counts, "by_state": by_state}


def _payload(v) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


@router.get("/requests")
async def acquire_requests(request: Request, limit: int = 15) -> dict:
    """Recent acquire requests + their resolution — the Request Queue drill-down. Each
    request links to its Mimir reply via the reply's dedup_key (acquirereply-<target_id>)."""
    pool = request.app.state.pool
    out = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT target_id, payload, emitted_at, status FROM events "
            "WHERE event_type = 'acquire.requested' ORDER BY id DESC LIMIT $1",
            min(limit, 50),
        )
        for r in rows:
            p = _payload(r["payload"])
            reply = await conn.fetchrow(
                "SELECT event_type, payload FROM events WHERE dedup_key = $1 ORDER BY id DESC LIMIT 1",
                f"acquirereply-{r['target_id']}",
            )
            rp = _payload(reply["payload"]) if reply else {}
            out.append(
                {
                    "requester": p.get("requester"),
                    "subject": p.get("query") or p.get("arxiv_id") or p.get("url") or p.get("doi") or "—",
                    "why": p.get("why"),
                    "at": r["emitted_at"].isoformat() if r["emitted_at"] else None,
                    "request_status": r["status"],
                    "outcome": rp.get("status") or ("pending" if not reply else reply["event_type"].split(".")[-1]),
                    "reason": rp.get("reason"),
                    "document_id": rp.get("document_id"),
                }
            )
    counts: dict[str, int] = {}
    for o in out:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    return {"requests": out, "counts": counts, "health": await _queue_health(pool)}


async def _queue_health(pool) -> dict:
    """Is the queue empty, flowing, or backing up? Depth = requests with no Mimir reply yet;
    lag = how long the OLDEST unanswered request has waited (a draining queue keeps this low —
    a stuck/slow Mimir or a down harness lets it climb). Plus last-hour throughput. Scoped to
    24h so ancient orphaned requests don't masquerade as live backlog."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH req AS (
                -- Genuine queue only: a SUPPRESSED request (mimir paused, cooldown,
                -- or a manual backlog clear) was SKIPPED and will never get a reply,
                -- so it must not count as "in queue" / inflate the oldest-wait lag.
                SELECT target_id, emitted_at FROM events
                WHERE event_type = 'acquire.requested'
                  AND status NOT IN ('suppressed', 'failed')
                  AND emitted_at > now() - interval '24 hours'
            ),
            pend AS (
                SELECT r.emitted_at FROM req r
                WHERE NOT EXISTS (SELECT 1 FROM events rep WHERE rep.dedup_key = 'acquirereply-' || r.target_id)
            )
            SELECT
                (SELECT count(*) FROM pend) AS pending,
                (SELECT EXTRACT(EPOCH FROM (now() - min(emitted_at)))::int FROM pend) AS oldest_pending_age,
                (SELECT count(*) FROM events WHERE event_type = 'acquire.requested'
                    AND status NOT IN ('suppressed', 'failed')
                    AND emitted_at > now() - interval '1 hour') AS requested_1h,
                (SELECT count(*) FROM events WHERE event_type IN ('acquire.fulfilled', 'acquire.rejected')
                    AND emitted_at > now() - interval '1 hour') AS resolved_1h
            """
        )
    return {
        "pending": row["pending"] or 0,
        "oldest_pending_age_seconds": row["oldest_pending_age"],  # null when nothing is pending
        "requested_1h": row["requested_1h"] or 0,
        "resolved_1h": row["resolved_1h"] or 0,
    }


def _loads_loose(s: str | None) -> dict:
    """Parse a model's raw output_summary into a dict — tolerant of ```fences / <think> preludes
    (the same shapes agents.llm._strip_fences handles). Returns {} on anything unparseable."""
    if not s:
        return {}
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_question(ask_input: str | None) -> str | None:
    """Pull just Ariadne's question out of the full Mimir prompt (the input_summary is
    '## system …\n\n## user\n# Question\n<the question>\n\n## Retrieved passages …')."""
    if not ask_input or "# Question" not in ask_input:
        return None
    after = ask_input.split("# Question", 1)[1].lstrip("\n :")
    for sep in ("\n\n## ", "\n\n# Task", "\n# Task"):
        if sep in after:
            after = after.split(sep, 1)[0]
    return after.strip() or None


def _deliberation_outcome(delib: dict) -> dict:
    """The agenda Ariadne FRAMED from a deliberation: mission + direction titles."""
    dirs = delib.get("directions") if isinstance(delib.get("directions"), list) else []
    return {
        "label": "Framed",
        "summary": delib.get("mission_frame"),
        "items": [d.get("title") for d in dirs if isinstance(d, dict) and d.get("title")][:6],
    }


def _reflection_outcome(reflect: dict) -> dict:
    """How Ariadne STEERED the standing agenda: her portfolio read + per-direction verdicts."""
    verdicts = reflect.get("verdicts") if isinstance(reflect.get("verdicts"), list) else []
    items = []
    for v in verdicts[:6]:
        if isinstance(v, dict) and v.get("assessment"):
            cid = v.get("claim_id")
            reason = (v.get("reason") or "").strip()
            items.append(f"#{cid} {v['assessment']}" + (f" — {reason[:90]}" if reason else ""))
    return {
        "label": "Steered",
        "summary": reflect.get("reprioritized_focus") or reflect.get("portfolio_assessment"),
        "items": items,
    }


@router.get("/conversations")
async def conversations(request: Request, limit: int = 12) -> dict:
    """Past Ariadne↔Mimir conversations as chat threads — both DELIBERATIONS (frame a fresh
    agenda) and REFLECTIONS (steer the standing one). Each: her multi-hop question to Mimir,
    Mimir's synthesized answer + flagged gaps + citations, and the outcome she reached. Sourced
    from the captured agent_runs (the `ask` step + the `deliberate`/`reflect` step of each
    session). Only runs whose transcript was recorded appear (older runs have none)."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT session_id,
               max(started_at)                                           AS at,
               max(input_summary)  FILTER (WHERE step_name = 'ask')        AS ask_input,
               max(output_summary) FILTER (WHERE step_name = 'ask')        AS ask_output,
               max(output_summary) FILTER (WHERE step_name = 'deliberate') AS delib_output,
               max(output_summary) FILTER (WHERE step_name = 'reflect')    AS reflect_output
        FROM agent_runs
        WHERE invocation_type IN ('mimir.ask', 'ariadne.deliberate', 'ariadne.reflect')
          AND session_id IS NOT NULL
        GROUP BY session_id
        HAVING max(output_summary) FILTER (WHERE step_name = 'ask') IS NOT NULL
        ORDER BY session_id DESC
        LIMIT $1
        """,
        min(limit, 50),
    )
    out = []
    for r in rows:
        mimir = _loads_loose(r["ask_output"])
        is_reflection = r["reflect_output"] is not None
        outcome = (
            _reflection_outcome(_loads_loose(r["reflect_output"]))
            if is_reflection
            else _deliberation_outcome(_loads_loose(r["delib_output"]))
        )
        out.append(
            {
                "session_id": r["session_id"],
                "at": r["at"].isoformat() if r["at"] else None,
                "kind": "reflection" if is_reflection else "deliberation",
                "question": _extract_question(r["ask_input"]),
                "answer": mimir.get("answer"),
                "citations": [c for c in (mimir.get("citations") or []) if isinstance(c, str)][:6],
                "gaps": [g for g in (mimir.get("gaps") or []) if isinstance(g, str)][:6],
                "outcome": outcome,
            }
        )
    return {"conversations": out}


@router.get("/planner")
async def planner_panel(request: Request) -> dict:
    """Planner drill-down: its mode, the research tasks it has produced (totals by status + the
    recent ones with the direction each came from), how many APPROVED directions are still
    awaiting a plan (its backlog), and when it last ran. The Planner turns Ariadne's approved
    directions into concrete, falsifiable research tasks (task.created → the Researchers)."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mode = await conn.fetchval("SELECT mode FROM agent_modes WHERE agent_name = 'planner'") or "off"
        by_status = {
            r["status"]: r["n"]
            for r in await conn.fetch(
                "SELECT status::text AS status, count(*) AS n FROM tasks WHERE department = 'research' GROUP BY status"
            )
        }
        last_plan = await conn.fetchval(
            "SELECT emitted_at FROM events WHERE event_type = 'planner.plan' ORDER BY id DESC LIMIT 1"
        )
        # Approved directions with no research task yet — what the Planner still has to plan.
        awaiting = await conn.fetchval(
            "SELECT count(*) FROM claims c JOIN direction_gate g ON g.claim_id = c.id "
            "WHERE c.claim_kind = 'direction' AND g.status = 'approved' "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id AND t.department = 'research')"
        )
        tasks = await conn.fetch(
            "SELECT t.id, t.task_type, t.description, t.status::text AS status, t.created_at, "
            "       c.statement AS direction "
            "FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id "
            "WHERE t.department = 'research' ORDER BY t.id DESC LIMIT 8"
        )
    return {
        "mode": mode,
        "tasks_total": sum(by_status.values()),
        "by_status": by_status,
        "awaiting_plan": awaiting or 0,
        "last_plan_at": last_plan.isoformat() if last_plan else None,
        "tasks": [
            {
                "id": t["id"],
                "task_type": t["task_type"],
                "description": t["description"],
                "status": t["status"],
                "direction": t["direction"],
                "at": t["created_at"].isoformat() if t["created_at"] else None,
            }
            for t in tasks
        ],
    }


class GateDecision(BaseModel):
    decision: str  # approved | held | rejected | pending
    note: str | None = None


@router.post("/gate/{claim_id}")
async def set_gate(request: Request, claim_id: int, body: GateDecision) -> dict:
    """The PRIORITY GATE — a human promotes/holds/rejects one of Ariadne's directions.
    Only 'approved' directions become active research (the Planner, Stage 2, will read this).
    Approving is capped at GATE_BUDGET so the lab can't over-commit."""
    pool = request.app.state.pool
    if body.decision not in _GATE_DECISIONS:
        return {"ok": False, "error": f"decision must be one of {_GATE_DECISIONS}"}
    async with pool.acquire() as conn:
        kind = await conn.fetchval("SELECT claim_kind FROM claims WHERE id = $1", claim_id)
        if kind != "direction":
            return {"ok": False, "error": f"claim {claim_id} is not a direction"}
        if body.decision == "approved":
            approved = await conn.fetchval(
                f"SELECT count(*) FROM direction_gate dg JOIN claims c ON c.id = dg.claim_id "
                f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' "
                f"AND c.status IN {_ACTIVE} AND dg.claim_id <> $1",
                claim_id,
            )
            if approved >= GATE_BUDGET:
                return {
                    "ok": False,
                    "error": f"budget full ({approved}/{GATE_BUDGET} approved) — hold or reject another direction first",
                    "budget_full": True,
                }
        await conn.execute(
            "INSERT INTO direction_gate (claim_id, status, note, decided_by, decided_at) "
            "VALUES ($1, $2, $3, 'human', now()) "
            "ON CONFLICT (claim_id) DO UPDATE SET status = $2, note = $3, decided_by = 'human', decided_at = now()",
            claim_id,
            body.decision,
            body.note,
        )
    log.info("ariadne gate: direction #%d -> %s", claim_id, body.decision)
    return {"ok": True, "claim_id": claim_id, "decision": body.decision}
