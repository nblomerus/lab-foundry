"""
Event-driven dispatcher.

Listens to Postgres NOTIFY on the 'events' channel, routes events to
registered handlers, enforces friction gates (cooldowns, cost caps, slop
pause), and persists handler outcomes back to the events table.

A periodic watchdog catches anything the event bus missed (stale tasks,
dropped notifies, phase budget overrun).

Handlers are async functions: (event_dict, dispatcher) -> dict | None.
They are registered by event_type via dispatcher.register(); the actual
handler implementations live in src/handlers/.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import asyncpg

log = logging.getLogger(__name__)

Handler = Callable[[dict, "Dispatcher"], Awaitable[Optional[dict]]]


# -------------------------------------------------------------------------
# Cooldown configuration
# -------------------------------------------------------------------------
# (event_type, target_type) -> cooldown configuration
# cooldown_s: how long to suppress repeat-fires
# bypass_on_events: if these events fired against the same target, bypass cooldown

COOLDOWNS: dict[tuple[str, str], dict] = {
    # (event_type, target_type) -> the invocation_type whose cooldown gates this
    # event. The cooldown rows live in the cooldowns table keyed by invocation_type.
    ("finding.high_signal",       "thesis"): {
        "invocation_type": "adversary.kill_verdict",
        "cooldown_s": 14_400,
    },
    ("thesis.confidence_changed", "thesis"): {
        "invocation_type": "phase_adjudicator.check",
        "cooldown_s": 1_800,
    },
    ("queue.empty",               "queue"):  {
        "invocation_type": "planner.generate_tasks",
        "cooldown_s": 600,
    },
}

# Events that ALWAYS bypass cooldowns and most gates
URGENT_EVENTS = frozenset({
    "thesis.invalidated",
    "audit.slop_detected",
    "phase.budget_exceeded",
    "phase.transition_proposed",
    "company.bootstrapped",
})

# Phase budget in days (1.5× → forcing function)
PHASE_BUDGET_DAYS = {
    "exploration": 10,
    "convergence": 7,
    "commitment":  3,
    "execution":   10,
}


# -------------------------------------------------------------------------
# Dispatcher
# -------------------------------------------------------------------------

class Dispatcher:
    def __init__(self, pool: asyncpg.Pool, max_concurrent_handlers: int = 4):
        self.pool = pool
        self._handlers: dict[str, Handler] = {}
        self._running = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._listener_conn: Optional[asyncpg.Connection] = None
        # Bound how many handlers run at once. Without this, a queue refill
        # (planner emitting a batch of task.created events) spawns one task
        # per event with no ceiling — a dozen concurrent local-LLM runs that
        # all show 'running' and, if the harness is killed mid-flight, all
        # orphan into 'failed' rows. The cap keeps the in-flight count honest
        # and shrinks the orphan blast radius. Actual GPU calls are serialized
        # further downstream by the router's GPULock.
        self.max_concurrent_handlers = max_concurrent_handlers
        self._handler_sem = asyncio.Semaphore(max_concurrent_handlers)
        # Serializes the liveness pump so the startup pass and the watchdog's
        # first pass don't both read deficit=N and each emit N triggers.
        self._revive_lock = asyncio.Lock()

    def register(self, event_type: str, handler: Handler) -> None:
        if event_type in self._handlers:
            log.warning("overwriting handler for %s", event_type)
        self._handlers[event_type] = handler

    async def run(self) -> None:
        """Main loop. Blocks until stop() is called."""
        self._running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        self._listener_conn = await self.pool.acquire()
        try:
            await self._listener_conn.add_listener("events", self._on_notify)
            log.info("dispatcher listening on 'events' channel")
            await self._reap_startup_orphans()
            async with self.pool.acquire() as conn:
                await self._revive_stranded_tasks(conn)
            await self._drain_pending()
            while self._running:
                await asyncio.sleep(60)
        finally:
            if self._listener_conn:
                await self._listener_conn.remove_listener("events", self._on_notify)
                await self.pool.release(self._listener_conn)
                self._listener_conn = None

    async def stop(self) -> None:
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()

    # -- Notify handler --------------------------------------------------

    def _on_notify(self, conn, pid, channel, payload):
        try:
            data = json.loads(payload)
            asyncio.create_task(self._process_event(data["id"]))
        except Exception:
            log.exception("failed to parse notify payload: %r", payload)

    async def _process_event(self, event_id: int) -> None:
        async with self.pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE id = $1 AND status = 'pending'",
                event_id,
            )
            if event is None:
                return
            event = dict(event)
            # JSONB comes back as a string without a codec; normalize to dict
            if isinstance(event.get("payload"), str):
                event["payload"] = json.loads(event["payload"]) if event["payload"] else {}

        handler = self._handlers.get(event["event_type"])
        if handler is None:
            await self._mark_suppressed(event_id, "no_handler")
            return

        is_urgent = event["event_type"] in URGENT_EVENTS

        if not is_urgent:
            async with self.pool.acquire() as conn:
                if await self._is_cooled_down(conn, event):
                    await self._mark_suppressed(event_id, "cooldown")
                    return
                if await self._is_cost_capped(conn):
                    await self._mark_suppressed(event_id, "cost_cap")
                    return
                if await self._is_slop_paused(conn, event):
                    await self._mark_suppressed(event_id, "slop_pause")
                    return

        # Run handler outside the claim transaction — it may take minutes.
        # Gate the execution behind the concurrency semaphore so a burst of
        # events doesn't launch unbounded handlers at once. Gate checks above
        # run first (and cheaply), so suppressed events never occupy a slot.
        async with self._handler_sem:
            try:
                result = await handler(event, self)
                await self._mark_consumed(event_id, handler.__name__, result)
            except Exception as e:
                log.exception("handler %s failed for event %s", handler.__name__, event_id)
                await self._mark_failed(event_id, str(e))

    # -- Event state transitions ----------------------------------------

    async def _mark_consumed(self, event_id: int, handler_name: str, result: Optional[dict]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'consumed',
                    consumed_at = NOW(),
                    consumed_by_handler = $1
                WHERE id = $2
                """,
                handler_name, event_id,
            )

    async def _mark_suppressed(self, event_id: int, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'suppressed',
                    consumed_at = NOW(),
                    suppression_reason = $1
                WHERE id = $2
                """,
                reason, event_id,
            )

    async def _mark_failed(self, event_id: int, error: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'failed',
                    consumed_at = NOW(),
                    suppression_reason = $1
                WHERE id = $2
                """,
                error[:500], event_id,
            )

    # -- Friction gates -------------------------------------------------

    async def _is_cooled_down(self, conn, event: dict) -> bool:
        """
        Look up the COOLDOWNS config for this event shape, then check the
        cooldowns table for an active cooldown on the *invocation* the event
        would trigger (not on the event_type itself).
        """
        key = (event["event_type"], event.get("target_type") or "")
        config = COOLDOWNS.get(key)
        if config is None or event.get("target_id") is None:
            return False
        existing = await conn.fetchval(
            """
            SELECT 1 FROM cooldowns
            WHERE invocation_type = $1
              AND target_type = $2
              AND target_id = $3
              AND cooldown_until > NOW()
            """,
            config["invocation_type"],
            event["target_type"],
            event["target_id"],
        )
        return existing is not None

    async def _is_cost_capped(self, conn) -> bool:
        row = await conn.fetchrow(
            "SELECT cap_reached FROM cost_tracking WHERE day = CURRENT_DATE"
        )
        return bool(row and row["cap_reached"])

    async def _is_slop_paused(self, conn, event: dict) -> bool:
        if event.get("target_type") != "thesis" or event.get("target_id") is None:
            return False
        slop_rate = await conn.fetchval(
            "SELECT slop_rate FROM slop_rate_by_thesis WHERE thesis_id = $1",
            event["target_id"],
        )
        return slop_rate is not None and slop_rate > 0.40

    async def set_cooldown(
        self,
        invocation_type: str,
        target_type: str,
        target_id: int,
        seconds: int,
        run_id: Optional[int] = None,
    ) -> None:
        """Public API: a handler sets a cooldown after it runs."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cooldowns (
                    invocation_type, target_type, target_id, cooldown_until, set_by_run_id
                )
                VALUES ($1, $2, $3, NOW() + ($4 || ' seconds')::INTERVAL, $5)
                ON CONFLICT (invocation_type, target_type, target_id) DO UPDATE
                SET cooldown_until = EXCLUDED.cooldown_until,
                    set_by_run_id = EXCLUDED.set_by_run_id
                """,
                invocation_type, target_type, target_id, str(seconds), run_id,
            )

    # -- Startup drain --------------------------------------------------

    async def _reap_startup_orphans(self) -> None:
        """
        A freshly started harness has no runs in flight, so any agent_runs
        still 'running' belong to a previous instance that died. Mark them
        failed right away instead of waiting on the 30-minute watchdog sweep —
        which never fires if the harness stayed down (the orphans then linger
        as phantom 'running' rows, inflating in-flight counts and poisoning
        duration averages until someone reaps them by hand).

        Tasks left 'running' are reset to 'pending' so their work resumes.
        """
        async with self.pool.acquire() as conn:
            runs = await conn.execute(
                """
                UPDATE agent_runs
                SET status = 'failed',
                    completed_at = NOW(),
                    error = COALESCE(error, '') || ' [orphan reaped at startup]'
                WHERE status = 'running'
                """
            )
            tasks = await conn.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    started_at = NULL,
                    claimed_by = NULL
                WHERE status = 'running'
                """
            )
        if runs != "UPDATE 0" or tasks != "UPDATE 0":
            log.info("startup orphan reap: agent_runs=%s, tasks=%s", runs, tasks)

    async def _drain_pending(self) -> None:
        """At startup, kick off processing for any events still pending."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM events WHERE status = 'pending' "
                "ORDER BY emitted_at LIMIT 100"
            )
        for r in rows:
            asyncio.create_task(self._process_event(r["id"]))

    # -- Liveness pump --------------------------------------------------

    async def _revive_stranded_tasks(self, conn) -> None:
        """
        Keep the work loop self-sustaining.

        A task only gets worked when its INSERT fires the trg_emit_task_created
        trigger. Tasks that re-enter 'pending' via an UPDATE — reset by the
        stale-task sweep, the startup reap, or a crash — never re-fire that
        trigger, and their original task.created event is already consumed, so
        re-emitting it with the same dedup_key is a no-op. They strand: pending
        forever with nothing to claim them, and the company silently flatlines.

        Fix: if there are more pending tasks than pending task.created events,
        emit enough fresh triggers (unique dedup_key) to cover the deficit.
        handle_task_created claims the next available task regardless of the
        event's target, so generic triggers are sufficient and self-balancing.

        Guarded by _revive_lock: the startup pass and the watchdog's immediate
        first pass would otherwise both read the same deficit and each emit it.
        Serializing means the second caller re-counts, sees the first's
        triggers already pending, and emits nothing.
        """
        async with self._revive_lock:
            pending_tasks = await conn.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status = 'pending'"
            )
            if not pending_tasks:
                return
            pending_triggers = await conn.fetchval(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type = 'task.created' AND status = 'pending'"
            )
            deficit = int(pending_tasks) - int(pending_triggers)
            if deficit <= 0:
                return
            ts = int(datetime.now(timezone.utc).timestamp())
            for i in range(deficit):
                await conn.execute(
                    """
                    INSERT INTO events (event_type, target_type, payload, dedup_key)
                    VALUES ('task.created', 'task', '{}'::jsonb, $1)
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    f"revive-{ts}-{i}",
                )
            log.info(
                "liveness pump: re-emitted %d task.created for %d stranded pending task(s)",
                deficit, pending_tasks,
            )

    # -- Watchdog -------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Every 5 minutes: sweep stale tasks, revive stranded work, missed events, budgets."""
        while self._running:
            try:
                async with self.pool.acquire() as conn:
                    await self._sweep_stale_tasks(conn)
                    await self._revive_stranded_tasks(conn)
                    await self._sweep_pending_events(conn)
                    await self._check_phase_budget(conn)
                    await self._refresh_slop_view(conn)
            except Exception:
                log.exception("watchdog sweep failed")
            await asyncio.sleep(300)

    async def _sweep_stale_tasks(self, conn) -> None:
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                started_at = NULL,
                claimed_by = NULL
            WHERE status = 'running'
              AND started_at < NOW() - INTERVAL '30 minutes'
            """
        )
        # Same idea for agent_runs: rows left in 'running' past the wall-clock
        # cap belong to processes that died. Mark them failed so the dashboard
        # doesn't lie about "in flight" counts.
        await conn.execute(
            """
            UPDATE agent_runs
            SET status = 'failed',
                completed_at = NOW(),
                error = COALESCE(error, '') || ' [orphan reaped by watchdog]'
            WHERE status = 'running'
              AND started_at < NOW() - INTERVAL '30 minutes'
            """
        )

    async def _sweep_pending_events(self, conn) -> None:
        rows = await conn.fetch(
            """
            SELECT id FROM events
            WHERE status = 'pending'
              AND emitted_at < NOW() - INTERVAL '2 minutes'
            ORDER BY emitted_at LIMIT 50
            """
        )
        for r in rows:
            asyncio.create_task(self._process_event(r["id"]))

    async def _check_phase_budget(self, conn) -> None:
        state = await conn.fetchrow(
            "SELECT current_phase, phase_started_at FROM company_state WHERE id = 1"
        )
        if state is None:
            return
        budget = PHASE_BUDGET_DAYS.get(state["current_phase"])
        if budget is None:
            return

        elapsed = (datetime.now(timezone.utc) - state["phase_started_at"]).days
        if elapsed > int(budget * 1.5):
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, payload, dedup_key)
                VALUES (
                    'phase.budget_exceeded',
                    'phase',
                    $1::jsonb,
                    'budget-' || $2
                )
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                json.dumps({"phase": state["current_phase"], "elapsed_days": elapsed}),
                state["current_phase"],
            )

    async def _refresh_slop_view(self, conn) -> None:
        await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY slop_rate_by_thesis")
