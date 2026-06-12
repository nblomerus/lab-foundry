"""
Ariadne's PACEMAKER — the condition-driven trigger that makes her operating loop run
itself (the diagram's "CONTINUOUS" loop) instead of waiting to be poked.

Per the lab's condition-driven philosophy, the PRIMARY signal is new knowledge: when
Mimir has grown the queryable corpus by a meaningful amount since her last run, the
landscape may have shifted and she should re-assess. Cooldowns are the only time-based
guards (anti-thrash). On each trigger it first refreshes the field model so she reads the
CURRENT landscape, then emits:

  * ariadne.deliberate — re-frame the whole agenda (rare: no mission yet, a big corpus
    jump, or the agenda is EXHAUSTED — every direction terminal — in which case growth is
    irrelevant and only the deliberate cooldown gates the re-frame).
    persist_directions supersedes the prior mission, so missions don't pile up.
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
# A BARREN deliberate (ran but persisted no mission/directions — e.g. failed grading even
# after the handler's corrective retry) gets a shorter exhaustion-retry than the full
# cooldown: the lab is parked, the failure was an output flake, waiting 2h compounds it.
DELIB_BARREN_RETRY_S = float(os.environ.get("ARIADNE_DELIB_BARREN_RETRY_S", str(30 * 60)))
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
# Drive every approved direction to at least this many completed experiments so it can be synthesized
# into a finding (matches SYNTHESIS_MIN_EXPERIMENTS). 0 disables the coverage driver.
EXPERIMENT_COVERAGE_TARGET = int(os.environ.get("EXPERIMENT_COVERAGE_TARGET", "3"))

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


async def _maybe_drive_experiments(pool) -> int:
    """Drive each APPROVED, active, non-concluded, non-HELD direction toward the experiment-coverage
    target. A direction needs >= SYNTHESIS_MIN experiments to be synthesized into a finding, but the
    planner caps tasks per direction and the researcher only flags `needs_experiment` sometimes — so
    most directions never reach the threshold and the finding/conclude pipeline starves. This requests
    the NEXT experiment for any under-target direction with nothing in flight: every approved direction
    marches to a finding (breadth), not just whichever one a researcher happened to flag. Serial per
    direction (the in-flight guard) → naturally fair; the QM's concurrency cap bounds the whole lane.
    The dedup round keys on TOTAL attempts (completed + failed) PLUS a 6h time bucket: attempts alone
    deadlocked — a requested event that died (handler crash, dial-off/cost-cap suppression) created no
    run, so attempts never changed and the same key blocked every retry forever. The bucket re-arms a
    dead request within 6h, while the in-flight guard keeps a LIVE request from double-firing (a
    designed experiment is queued → inflight > 0 → skip). Give-up cap unchanged. Held-by-adjudication
    directions are skipped (don't spend compute on work the independent reviewer flagged as redundant)."""
    emitted = 0
    async with pool.acquire() as conn:
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
                      ORDER BY t.id DESC LIMIT 1) AS task_id
            FROM claims c JOIN direction_gate dg ON dg.claim_id = c.id
            WHERE dg.status = 'approved' AND c.claim_kind = 'direction' AND c.status IN {_ACTIVE}
              AND NOT EXISTS (
                  SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'hold'
              )
            """
        )
        for r in rows:
            if (
                r["done"] >= EXPERIMENT_COVERAGE_TARGET
                or r["inflight"] > 0
                or r["task_id"] is None
                or r["attempts"] >= EXPERIMENT_COVERAGE_TARGET * 3  # give-up cap: a direction that can't produce runs
            ):
                continue
            res = await conn.execute(
                "INSERT INTO events (event_type, target_type, target_id, payload, status, dedup_key) "
                "VALUES ('experiment.requested', 'claim', $1, $2::jsonb, 'pending', $3) "
                "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
                r["id"],
                json.dumps({"claim_id": r["id"], "task_id": r["task_id"], "trigger": "coverage"}),
                f"drive-exp-{r['id']}-{r['attempts']}-{int(time.time() // 21600)}",
            )
            if res.endswith(" 1"):
                emitted += 1
    if emitted:
        log.info("ariadne pace: drove %d direction(s) toward the experiment-coverage target", emitted)
    return emitted


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


async def _flag_agenda_exhausted(pool, mission_id: int, corpus: int, retry_in_s: float, now, *, active: int = 0) -> None:
    """loop.unclosed [agenda_exhausted] — a mission with ZERO live directions (or only
    unactionable held ones: `active` > 0) stalls every downstream lane (plan → research →
    experiment → synthesize) until a deliberation restocks the agenda; flag it hourly so
    lab_doctor/closure_audit name the culprit. Sentinel target (target_id 0, mirroring
    dispatch._emit_indicator) because a NULL target_id never conflicts (Postgres NULLs
    are distinct) and the hourly dedup would be decorative."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (event_type, target_type, target_id, payload, dedup_key) "
            "VALUES ('loop.unclosed', 'system', 0, $1::jsonb, $2) "
            "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
            json.dumps(
                {
                    "kind": "agenda_exhausted",
                    "mission_id": mission_id,
                    "corpus": corpus,
                    "active_unactionable": active,
                    "deliberate_retry_in_s": int(retry_in_s),
                }
            ),
            f"agenda-exhausted-{now:%Y-%m-%dT%H}",
        )


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
        # Live directions of ANY gate status — zero means the agenda is exhausted (every
        # direction invalidated/superseded/graduated) and nothing downstream can run.
        active_directions = await conn.fetchval(
            f"SELECT count(*) FROM claims WHERE claim_kind='direction' AND status IN {_ACTIVE}"
        )
        # Directions that can still MOVE the lab: gate-approved (workable), awaiting the
        # independent adjudicator (pipeline in progress), or adjudicated 'pass' (auto-
        # approvable). An agenda where every live direction is HELD is exhausted in all
        # but name — the planner won't plan it, the driver won't experiment on it, and
        # auto-approve requires a 'pass' — yet it kept the empty-agenda hatch from firing
        # (observed 2026-06-12: adjudicator held all 3 fresh directions on real prior art).
        actionable = await conn.fetchval(
            f"SELECT count(*) FROM claims c WHERE c.claim_kind='direction' AND c.status IN {_ACTIVE} AND ("
            f"EXISTS (SELECT 1 FROM direction_gate dg WHERE dg.claim_id=c.id AND dg.status='approved') "
            f"OR NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id=c.id) "
            f"OR EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id=c.id AND da.verdict='pass'))"
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
        # State-derived "did the last deliberate actually produce an agenda?" — if no
        # mission/direction claim was created after it fired, it was BARREN (failed
        # grading / crashed) and the exhaustion branch may retry on the shorter cooldown.
        last_agenda_at = await conn.fetchval(
            "SELECT max(created_at) FROM claims WHERE claim_kind IN ('mission','direction')"
        )

    def grew_since(row):
        base = _corpus_of(row)
        return corpus - base if isinstance(base, (int, float)) else corpus

    def age(row):
        return (now - row["emitted_at"]).total_seconds() if row else 1e12

    # Agenda EXHAUSTED: a mission exists but zero live directions remain — OR every live
    # direction is unactionable (none approved, none awaiting adjudication, none passed:
    # all held). Reflect no-ops on an empty agenda, and once intake has relaxed to the 6h
    # agenda cadence the growth gate below may never trip — without this escape hatch the
    # lab parks silently (observed 2026-06-12 twice: 43/43 directions terminal, then a
    # fresh agenda held wholesale by the adjudicator). Growth is meaningless with nothing
    # workable to steer, so re-frame on the deliberate cooldown alone (anti-thrash for a
    # deliberation that restocks nothing) and skip the pointless reflect. The forced
    # deliberation supersedes the held directions via the normal persist path; a human can
    # still gate-approve a held direction any time before it fires. approved_active == 0
    # is required in the all-held arm — never re-frame over committed in-flight work.
    if mission is not None and (active_directions == 0 or (approved_active == 0 and actionable == 0)):
        why = (
            "0 live directions"
            if active_directions == 0
            else f"{active_directions} live, none actionable (all held/unapproved)"
        )
        # A barren last deliberate (nothing persisted after it fired) retries sooner — the
        # flake already cost the lab idle time; don't bill it the full re-frame cooldown.
        barren = last_delib is not None and (last_agenda_at is None or last_agenda_at < last_delib["emitted_at"])
        cooldown = DELIB_BARREN_RETRY_S if barren else DELIB_COOLDOWN_S
        retry_in = max(0.0, cooldown - age(last_delib))
        await _flag_agenda_exhausted(pool, mission, corpus, retry_in, now, active=active_directions)
        if age(last_delib) >= cooldown:
            barren_note = ", last was barren" if barren else ""
            log.warning("ariadne pace: agenda EXHAUSTED (%s%s) — forcing deliberate", why, barren_note)
            return "ariadne.deliberate", corpus
        log.warning("ariadne pace: agenda EXHAUSTED (%s) — deliberate in %.0fs (cooldown)", why, retry_in)
        return None, corpus

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
            # Drive every approved direction to the experiment-coverage target so it can reach a
            # finding — but only when the experiments agent can actually run them.
            if EXPERIMENT_COVERAGE_TARGET > 0 and await get_agent_mode(pool, "experiments") in {"advisory", "active"}:
                await _maybe_drive_experiments(pool)
            event_type, corpus = await _decide(pool)
            if event_type:
                await _refresh_field_model(pool)  # she reads the CURRENT landscape
                await _emit(pool, event_type, corpus)
                log.info("ariadne pace: emitted %s (corpus=%d)", event_type, corpus)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the pacemaker
            log.exception("ariadne pace: tick failed")
    log.info("ariadne pacemaker stopped")
