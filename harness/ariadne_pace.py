"""
Ariadne's PACEMAKER — the condition-driven trigger that makes her operating loop run
itself (the diagram's "CONTINUOUS" loop) instead of waiting to be poked.

Per the lab's condition-driven philosophy, the PRIMARY signal is new knowledge: when
Mimir has grown the queryable corpus by a meaningful amount since her last run, the
landscape may have shifted and she should re-assess. Cooldowns are the only time-based
guards (anti-thrash). On each trigger it first refreshes the field model so she reads the
CURRENT landscape, then emits:

  * ariadne.deliberate — re-frame the whole agenda (rare: no mission yet, or a big corpus
    jump). persist_directions supersedes the prior mission, so missions don't pile up.
  * ariadne.reflect    — steer the standing agenda (the regular beat).

Gated on ARIADNE_PACE (env, default OFF), mirroring the other *_LOOP gates. It only fires
when ariadne's mode dial is advisory|active — the dial still pauses her.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from harness.agent_modes import get_agent_mode
from library.graph.field_model import build_field_model
from library.graph.tools import _get_driver

log = logging.getLogger(__name__)

PACE_INTERVAL_S = float(os.environ.get("ARIADNE_PACE_INTERVAL_S", "600"))  # how often to check
DELIB_GROWTH = int(os.environ.get("ARIADNE_DELIB_GROWTH", "3000"))  # new docs → re-frame
DELIB_COOLDOWN_S = float(os.environ.get("ARIADNE_DELIB_COOLDOWN_S", str(2 * 3600)))
REFLECT_GROWTH = int(os.environ.get("ARIADNE_REFLECT_GROWTH", "800"))  # new docs → reflect
REFLECT_COOLDOWN_S = float(os.environ.get("ARIADNE_REFLECT_COOLDOWN_S", str(45 * 60)))
REFLECT_MAX_AGE_S = float(os.environ.get("ARIADNE_REFLECT_MAX_AGE_S", str(6 * 3600)))
# Hands-off autonomy: she approves her own top directions (decided_by='auto') up to the budget.
AUTO_APPROVE = os.environ.get("ARIADNE_AUTO_APPROVE", "").lower() in {"on", "1", "true"}
AUTO_APPROVE_MIN = float(os.environ.get("ARIADNE_AUTO_APPROVE_MIN_COMPOSITE", "3.5"))
# Per-dimension floors a direction must ALSO clear — composite alone let a high novelty
# score carry an inconsequential direction through. impact = it changes a real decision;
# novelty = it's not a confirmation; paper_potential = it's a publishable contribution.
# NULL impact (legacy/pre-impact rows) fails `>=` → won't auto-approve until re-deliberated.
GATE_IMPACT_MIN = int(os.environ.get("ARIADNE_GATE_IMPACT_MIN", "3"))
GATE_NOVELTY_MIN = int(os.environ.get("ARIADNE_GATE_NOVELTY_MIN", "3"))
GATE_PAPER_MIN = int(os.environ.get("ARIADNE_GATE_PAPER_MIN", "3"))
GATE_BUDGET = int(os.environ.get("ARIADNE_GATE_BUDGET", "3"))
# Require the INDEPENDENT adjudicator (agents/novelty) to pass a direction before auto-approval —
# the self-scores above are graded by the proposer; this is the external check. Default on; an
# un-adjudicated direction then never auto-approves (the fail-safe), so the novelty agent must run.
GATE_REQUIRE_ADJUDICATION = os.environ.get("ARIADNE_GATE_REQUIRE_ADJUDICATION", "on").lower() in {"on", "1", "true"}

_ACTIVE = "('proposed','tested','weakly_supported','replicated')"


async def _auto_approve(pool) -> int:
    """Hands-off gate: approve the top scored directions up to the budget (decided_by='auto')."""
    if not AUTO_APPROVE:
        return 0
    async with pool.acquire() as conn:
        approved = await conn.fetchval(
            f"SELECT count(*) FROM direction_gate dg JOIN claims c ON c.id = dg.claim_id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {_ACTIVE}"
        )
        slots = GATE_BUDGET - approved
        if slots <= 0:
            return 0
        # The independent adjudicator must have passed it — the external check on the self-scores.
        # An un-adjudicated or held direction is excluded (fail-safe) while adjudication is required.
        adj_clause = (
            "AND EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'pass') "
            if GATE_REQUIRE_ADJUDICATION
            else ""
        )
        rows = await conn.fetch(
            f"SELECT c.id FROM claims c JOIN direction_scores ds ON ds.claim_id = c.id "
            f"LEFT JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE c.claim_kind = 'direction' AND c.status IN {_ACTIVE} "
            f"AND (dg.status IS NULL OR dg.status = 'pending') "
            f"AND ds.composite >= $1 AND ds.impact >= $3 AND ds.novelty >= $4 AND ds.paper_potential >= $5 "
            f"{adj_clause}"
            f"ORDER BY ds.impact DESC, ds.composite DESC LIMIT $2",
            AUTO_APPROVE_MIN,
            slots,
            GATE_IMPACT_MIN,
            GATE_NOVELTY_MIN,
            GATE_PAPER_MIN,
        )
        for r in rows:
            await conn.execute(
                "INSERT INTO direction_gate (claim_id, status, note, decided_by, decided_at) "
                "VALUES ($1, 'approved', 'auto-approved (top by composite)', 'auto', now()) "
                "ON CONFLICT (claim_id) DO UPDATE SET status = 'approved', decided_by = 'auto', decided_at = now()",
                r["id"],
            )
    if rows:
        log.info("ariadne pace: auto-approved %d direction(s)", len(rows))
    return len(rows)


async def _maybe_adjudicate(pool) -> bool:
    """Emit direction.adjudicate when scored directions still lack an independent verdict (and
    none is queued) — so the gate has the external novelty/impact check it requires."""
    async with pool.acquire() as conn:
        unadjudicated = await conn.fetchval(
            f"SELECT count(*) FROM claims c JOIN direction_scores ds ON ds.claim_id = c.id "
            f"WHERE c.claim_kind = 'direction' AND c.status IN {_ACTIVE} "
            f"AND NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id)"
        )
        if not unadjudicated:
            return False
        if await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type = 'direction.adjudicate' AND status = 'pending'"
        ):
            return False
        await conn.execute(
            "INSERT INTO events (event_type, payload, status, dedup_key) "
            "VALUES ('direction.adjudicate', '{}'::jsonb, 'pending', $1)",
            f"pace-adjudicate-{int(time.time())}",
        )
    log.info("ariadne pace: emitted direction.adjudicate (%d scored direction(s) unadjudicated)", unadjudicated)
    return True


async def _maybe_plan(pool) -> bool:
    """Emit planner.plan when approved directions have no tasks yet (and none is queued)."""
    async with pool.acquire() as conn:
        unplanned = await conn.fetchval(
            f"SELECT count(*) FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {_ACTIVE} "
            f"AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id)"
        )
        if not unplanned:
            return False
        if await conn.fetchval("SELECT count(*) FROM events WHERE event_type = 'planner.plan' AND status = 'pending'"):
            return False
        await conn.execute(
            "INSERT INTO events (event_type, payload, status, dedup_key) "
            "VALUES ('planner.plan', '{}'::jsonb, 'pending', $1)",
            f"pace-plan-{int(time.time())}",
        )
    log.info("ariadne pace: emitted planner.plan (%d approved direction(s) unplanned)", unplanned)
    return True


def _corpus_of(row) -> float | None:
    """Read the corpus snapshot from an event payload (jsonb may arrive as str or dict)."""
    if not row:
        return None
    p = row["payload"]
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return None
    return p.get("corpus") if isinstance(p, dict) else None


async def _refresh_field_model(pool) -> None:
    try:
        driver = await _get_driver()
        s = await build_field_model(driver, pool)
        log.info(
            "ariadne pace: field model refreshed (%d concepts, cohorts %s→%s)", s["concepts"], s["prior"], s["recent"]
        )
    except Exception as e:  # noqa: BLE001 — best-effort; deliberation falls back to flat counts
        log.warning("ariadne pace: field model refresh failed: %s", e)


async def _emit(pool, event_type: str, corpus: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (event_type, payload, status, dedup_key) "
            "VALUES ($1, $2, 'pending', $3) "
            "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
            event_type,
            json.dumps({"trigger": "pace", "corpus": corpus}),
            f"pace-{event_type}-{corpus}",
        )


async def _decide(pool) -> tuple[str | None, int]:
    """Return (event_type_to_emit | None, current_corpus)."""
    async with pool.acquire() as conn:
        now = await conn.fetchval("SELECT now()")
        corpus = await conn.fetchval("SELECT count(*) FROM documents WHERE queryable")
        mission = await conn.fetchval(
            f"SELECT id FROM claims WHERE claim_kind='mission' AND status IN {_ACTIVE} ORDER BY id DESC LIMIT 1"
        )
        # Approved directions in flight = committed work; never re-frame over it (gate durability).
        approved_active = await conn.fetchval(
            f"SELECT count(*) FROM direction_gate dg JOIN claims c ON c.id = dg.claim_id "
            f"WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {_ACTIVE}"
        )
        # Don't stack triggers if one is already queued.
        if await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type IN ('ariadne.deliberate','ariadne.reflect') "
            "AND status='pending'"
        ):
            return None, corpus
        last_delib = await conn.fetchrow(
            "SELECT emitted_at, payload FROM events WHERE event_type='ariadne.deliberate' ORDER BY id DESC LIMIT 1"
        )
        last_reflect = await conn.fetchrow(
            "SELECT emitted_at, payload FROM events WHERE event_type='ariadne.reflect' ORDER BY id DESC LIMIT 1"
        )

    def grew_since(row):
        base = _corpus_of(row)
        return corpus - base if isinstance(base, (int, float)) else corpus

    def age(row):
        return (now - row["emitted_at"]).total_seconds() if row else 1e12

    # Deliberate: bootstrap, or a big corpus jump after the cooldown — but NEVER while approved
    # directions are in flight (don't re-frame committed work; keeps gate/auto-approve durable).
    if mission is None or (
        approved_active == 0 and grew_since(last_delib) >= DELIB_GROWTH and age(last_delib) >= DELIB_COOLDOWN_S
    ):
        return "ariadne.deliberate", corpus
    # Reflect: the regular beat — after the cooldown, on growth or a max-age tick.
    if age(last_reflect) >= REFLECT_COOLDOWN_S and (
        grew_since(last_reflect) >= REFLECT_GROWTH or age(last_reflect) >= REFLECT_MAX_AGE_S
    ):
        return "ariadne.reflect", corpus
    return None, corpus


async def ariadne_pacemaker(pool, stop: asyncio.Event) -> None:
    log.info(
        "ariadne pacemaker started (interval=%.0fs, delib_growth=%d, reflect_growth=%d)",
        PACE_INTERVAL_S,
        DELIB_GROWTH,
        REFLECT_GROWTH,
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PACE_INTERVAL_S)
            break  # stop was set
        except TimeoutError:
            pass  # a tick elapsed
        try:
            if await get_agent_mode(pool, "ariadne") not in {"advisory", "active"}:
                continue  # the mode dial pauses her
            await _maybe_adjudicate(pool)  # independent novelty/impact check before the gate
            await _auto_approve(pool)  # hands-off: approve her own top directions (adjudication-gated)
            await _maybe_plan(pool)  # trigger the Planner for approved-but-unplanned directions
            event_type, corpus = await _decide(pool)
            if event_type:
                await _refresh_field_model(pool)  # she reads the CURRENT landscape
                await _emit(pool, event_type, corpus)
                log.info("ariadne pace: emitted %s (corpus=%d)", event_type, corpus)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the pacemaker
            log.exception("ariadne pace: tick failed")
    log.info("ariadne pacemaker stopped")
