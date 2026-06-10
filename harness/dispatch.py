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
import contextvars
import itertools
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime

import asyncpg

from harness.agent_modes import agent_of, get_agent_mode, should_run
from harness.session import Session

log = logging.getLogger(__name__)

Handler = Callable[[dict, "Dispatcher"], Awaitable[dict | None]]


# Per-task session handle. Each handler invocation runs as its own asyncio
# task spawned in _on_notify → _process_event; asyncio.create_task copies the
# contextvars context at creation time, so setting this var inside one
# handler's process never leaks into a concurrent handler. Access from
# handler code is via `dispatcher.session`.
_current_session: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "labfoundry_current_session", default=None
)


# -------------------------------------------------------------------------
# Cooldown configuration
# -------------------------------------------------------------------------
# (event_type, target_type) -> cooldown configuration
# cooldown_s: how long to suppress repeat-fires
# bypass_on_events: if these events fired against the same target, bypass cooldown

COOLDOWNS: dict[tuple[str, str], dict] = {
    # (event_type, target_type) -> the invocation_type whose cooldown gates this
    # event. The cooldown rows live in the cooldowns table keyed by invocation_type.
    ("finding.high_signal", "claim"): {
        "invocation_type": "critic.attack",
        "cooldown_s": 14_400,
    },
    ("claim.confidence_changed", "claim"): {
        "invocation_type": "phase_adjudicator.check",
        "cooldown_s": 1_800,
    },
    ("queue.empty", "queue"): {
        "invocation_type": "planner.generate_tasks",
        "cooldown_s": 600,
    },
}

# Events that ALWAYS bypass cooldowns and most gates
URGENT_EVENTS = frozenset(
    {
        "claim.invalidated",
        "audit.slop_detected",
        "phase.budget_exceeded",
        "phase.transition_proposed",
        "company.bootstrapped",
    }
)

# Phase budget in days (1.5× → forcing function)
PHASE_BUDGET_DAYS = {
    "frame": 10,
    "hypothesize": 7,
    "experiment": 14,
    "validate": 7,
    "write": 7,
    "submit": 3,
}


# -------------------------------------------------------------------------
# Stall / liveness guards
# -------------------------------------------------------------------------
# Each handler runs inside a bounded concurrency pool (max_concurrent_handlers).
# If one HANGS it holds its slot forever — and the watchdog's stale-row reap
# can't help: it only rewrites the agent_runs ROW, it cannot cancel a live
# coroutine. Enough hung handlers and the whole dispatcher wedges while the DB
# reads idle (the indicators lie; only the external restart recovers it). So we
# bound every handler with a hard wall-clock timeout — on overrun
# asyncio.wait_for CANCELS the coroutine, freeing the slot, and we flag it.
# Default 30m matches the stale-task / orphan-run reap horizon so the three
# mechanisms agree. Env-override for any legitimately longer handler.
HANDLER_TIMEOUT_S = float(os.environ.get("HANDLER_TIMEOUT_S", "1800"))
# Soft warn: a handler still in flight past this is flagged (agent.slow) BEFORE
# the hard cancel, so a degrading agent surfaces early, not at the cliff.
HANDLER_SLOW_WARN_S = float(os.environ.get("HANDLER_SLOW_WARN_S", "600"))
# Saturation: when every slot is occupied AND at least this many events are
# backed up, the lab is held up behind the in-flight agents (dispatch.saturated).
SATURATION_BACKLOG = int(os.environ.get("DISPATCH_SATURATION_BACKLOG", "5"))
# Broken: an agent with >= this many recent runs, ALL failed (0 completed in the
# last hour), is flagged broken — it's silently flatlining its slice of the loop.
BROKEN_AGENT_MIN_RUNS = int(os.environ.get("BROKEN_AGENT_MIN_RUNS", "3"))


# -------------------------------------------------------------------------
# Closure guard — "work ran but the loop never closed"
# -------------------------------------------------------------------------
# An event-driven lab fails SILENTLY when something is produced but the consumer
# that should advance it never runs (a direction worked then left mid-loop, a
# fetched corpus that never re-triggers its requester, an event whose handler is
# unregistered in the current run mode). `no_handler` was treated as benign, so
# every such dead-end hid. The closure guard makes non-closure (a) always VISIBLE
# (loop.unclosed indicators + lab_doctor) and (b) auto-RESOLVED for the research
# ladder below.
#
# Events that are intentionally NOT dispatched to a handler — flagging these as
# non-closure would cry wolf. Two kinds: lifecycle/telemetry (streamed to the UI,
# never handled) and poll-consumed (read by an agent's own next run, not event-
# driven). Anything emitted and landing `no_handler` that is NOT here is a wiring
# regression (a real loop-closing event silently dropped) → flagged.
CLOSURE_EXEMPT_EVENTS = frozenset(
    {
        # lifecycle / telemetry (streamed, never handled by design)
        "session.started",
        "session.completed",
        "session.failed",
        "step.started",
        "step.completed",
        "step.failed",
        "document.parsed",
        "document.ingested",
        "document.staged",
        "library.ingest_rejected",
        "mimir.ingest_blocked",
        "library.trends",
        "cost.cap_reached",
        "lessons.reconciled",
        # the guard's own indicators (must never recurse into themselves)
        "agent.stalled",
        "agent.slow",
        "agent.broken",
        "dispatch.saturated",
        "loop.unclosed",
        # direct-call ask channel — handled by ops.mimir_ask, not the dispatcher
        "mimir.ask",
        "mimir.answered",
        # poll-consumed: the researcher's next run reads these (feedback.refine_disposition)
        "acquire.fulfilled",
        "acquire.rejected",
        # market-PI lifecycle — DELIBERATELY unhandled (Stage 0 neutralization, see harness/main.py).
        # Dead by design, not a wiring regression; the closure ladder also emits claim.invalidated.
        "claim.invalidated",
        "claim.confidence_changed",
        "phase.transition_proposed",
        "phase.budget_exceeded",
    }
)
# How long a non-exempt event type may sit `no_handler` in the window before it's
# flagged (so a one-off during a restart doesn't trip it).
UNCLOSED_EVENT_MIN = int(os.environ.get("CLOSURE_UNCLOSED_EVENT_MIN", "3"))

# Auto-close ladder (research directions): thin_corpus → acquire → ONE targeted
# scout sweep → re-attempt → still-thin ⇒ genuine gap (invalidate). All bounds are
# generous and env-tunable; the ladder is idempotent (one step per eligible tick).
ACTIVE_CLAIM = ("proposed", "tested", "weakly_supported", "replicated")
SCOUT_SETTLE_MIN = float(os.environ.get("CLOSURE_SCOUT_SETTLE_MIN", "20"))  # let a scout sweep ingest
CLOSURE_LOOKBACK_DAYS = int(os.environ.get("CLOSURE_LOOKBACK_DAYS", "3"))


# -------------------------------------------------------------------------
# Dispatcher
# -------------------------------------------------------------------------


class Dispatcher:
    def __init__(self, pool: asyncpg.Pool, max_concurrent_handlers: int = 4):
        self.pool = pool
        self._handlers: dict[str, Handler] = {}
        self._running = False
        self._watchdog_task: asyncio.Task | None = None
        self._listener_conn: asyncpg.Connection | None = None
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
        # Lessons reconciliation runs hourly (the watchdog ticks every 5 min);
        # this stamps the last run so we don't reconcile on every tick.
        self._last_lessons_tick: datetime | None = None
        # Agenda-mode library sweep cadence (only used once Ariadne is active);
        # stamped here so the 5-min watchdog doesn't kick a sweep every tick.
        self._last_sweep_tick: datetime | None = None
        # Continuous library-intake pump (aggressive base-building while Ariadne
        # is dark). Condition-driven, not interval-driven — see _discovery_pump.
        self._pump_task: asyncio.Task | None = None
        self._last_pump_emit: datetime | None = None
        # Live in-flight handler registry: token -> {agent, handler, event_id,
        # started_at(monotonic)}. The authoritative view of what is holding a
        # concurrency slot RIGHT NOW — the watchdog reads it to flag slow
        # handlers and saturation, which the DB (reaped/stale rows) can't show.
        self._inflight: dict[int, dict] = {}
        self._inflight_seq = itertools.count(1)

    def register(self, event_type: str, handler: Handler) -> None:
        if event_type in self._handlers:
            log.warning("overwriting handler for %s", event_type)
        self._handlers[event_type] = handler

    @property
    def session(self) -> Session | None:
        """The Session for the currently-executing handler (None outside one).

        Per-task contextvar — concurrent handlers each see their own session
        without explicit threading. Handlers pass it through to
        `router.invoke(session=dispatcher.session, step_name=...)` so each
        multi-step invocation lights up as a DAG in /trace.
        """
        return _current_session.get()

    async def run(self) -> None:
        """Main loop. Blocks until stop() is called."""
        self._running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self._pump_task = asyncio.create_task(self._discovery_pump_loop())

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
        if self._pump_task:
            self._pump_task.cancel()

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

        # Per-agent mode dial (Stage 4 / debug control): run only when advisory|active.
        # off|shadow pause the agent — applies even to URGENT events (a deliberate pause
        # must hold regardless of urgency). System handlers (agent_of -> None) never gate.
        agent = agent_of(handler)
        if agent is not None:
            mode = await get_agent_mode(self.pool, agent)
            if not should_run(mode):
                await self._mark_suppressed(event_id, f"agent_{mode}")
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
            session = Session(
                handler_name=handler.__name__,
                triggered_by_event_id=event_id,
            )
            try:
                await session.start(self.pool)
            except Exception:
                # Session bookkeeping failure must not block the handler —
                # session.id stays 0 and router.invoke skips step linkage,
                # behaving exactly like the pre-session path.
                log.exception("session.start failed for event %s; running without trace", event_id)
            token = _current_session.set(session)
            inflight_id = next(self._inflight_seq)
            self._inflight[inflight_id] = {
                "agent": agent or handler.__name__,
                "handler": handler.__name__,
                "event_id": event_id,
                "started_at": time.monotonic(),
            }
            try:
                # Hard wall-clock bound: on overrun wait_for CANCELS the handler
                # coroutine, so it can never hold its concurrency slot forever
                # (the watchdog's row reap can't do that — it only rewrites rows).
                result = await asyncio.wait_for(handler(event, self), timeout=HANDLER_TIMEOUT_S)
                await self._mark_consumed(event_id, handler.__name__, result)
                await session.finish("completed")
            except TimeoutError:
                # wait_for already cancelled the handler; the slot frees the moment
                # we leave this block. Flag the stall so it's visible, not silent.
                log.error(
                    "handler %s TIMED OUT after %.0fs on event %s — cancelled (slot freed)",
                    handler.__name__,
                    HANDLER_TIMEOUT_S,
                    event_id,
                )
                await self._mark_failed(event_id, f"handler timed out after {HANDLER_TIMEOUT_S:.0f}s (cancelled)")
                await self._emit_indicator(
                    "agent.stalled",
                    {
                        "agent": agent or handler.__name__,
                        "handler": handler.__name__,
                        "event_id": event_id,
                        "timeout_s": HANDLER_TIMEOUT_S,
                        "action": "cancelled",
                    },
                    dedup=f"stalled-{agent or handler.__name__}-{event_id}",
                )
                with suppress(Exception):
                    await session.finish("failed", error="handler timeout")
            except Exception as e:
                log.exception("handler %s failed for event %s", handler.__name__, event_id)
                await self._mark_failed(event_id, str(e))
                try:
                    await session.finish("failed", error=str(e))
                except Exception:
                    log.exception("session.finish failed for event %s", event_id)
            finally:
                self._inflight.pop(inflight_id, None)
                _current_session.reset(token)

    # -- Event state transitions ----------------------------------------

    async def _mark_consumed(self, event_id: int, handler_name: str, result: dict | None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'consumed',
                    consumed_at = NOW(),
                    consumed_by_handler = $1
                WHERE id = $2
                """,
                handler_name,
                event_id,
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
                reason,
                event_id,
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
                error[:500],
                event_id,
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
        row = await conn.fetchrow("SELECT cap_reached FROM cost_tracking WHERE day = CURRENT_DATE")
        return bool(row and row["cap_reached"])

    async def _is_slop_paused(self, conn, event: dict) -> bool:
        if event.get("target_type") != "claim" or event.get("target_id") is None:
            return False
        slop_rate = await conn.fetchval(
            "SELECT slop_rate FROM slop_rate_by_claim WHERE claim_id = $1",
            event["target_id"],
        )
        return slop_rate is not None and slop_rate > 0.40

    async def set_cooldown(
        self,
        invocation_type: str,
        target_type: str,
        target_id: int,
        seconds: int,
        run_id: int | None = None,
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
                invocation_type,
                target_type,
                target_id,
                str(seconds),
                run_id,
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
            rows = await conn.fetch("SELECT id FROM events WHERE status = 'pending' ORDER BY emitted_at LIMIT 100")
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
            pending_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'pending'")
            if not pending_tasks:
                return
            pending_triggers = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE event_type = 'task.created' AND status = 'pending'"
            )
            deficit = int(pending_tasks) - int(pending_triggers)
            if deficit <= 0:
                return
            ts = int(datetime.now(UTC).timestamp())
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
                deficit,
                pending_tasks,
            )

    # -- Stall / broken-agent indicators --------------------------------

    async def _emit_indicator(self, event_type: str, payload: dict, *, dedup: str) -> None:
        """Write a telemetry/indicator event. No handler is registered for these,
        so they self-suppress as 'no_handler' (the same convention cost.cap_reached
        uses) — the point is the row, which /events + lab_doctor read. target_id=0
        is the sentinel so the UNIQUE (event_type,target_type,target_id,dedup_key)
        dedups (a NULL target_id would defeat it). Best-effort: telemetry must never
        break the loop it is observing."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                    VALUES ($1, 'agent', 0, $2::jsonb, $3)
                    ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                    """,
                    event_type,
                    json.dumps(payload),
                    dedup,
                )
            log.warning("indicator %s: %s", event_type, json.dumps(payload))
        except Exception:  # noqa: BLE001 — an indicator failing must not sink the watchdog
            log.exception("failed to emit indicator %s", event_type)

    async def _detect_stalls(self) -> None:
        """Read the LIVE in-flight registry (not the DB) and surface what the
        reaped-row view can't: (1) any handler past the soft-warn age → agent.slow,
        an early warning before the hard cancel; (2) every slot occupied while
        events back up → dispatch.saturated, i.e. the lab is held up behind these
        agents. Both are deduped on a coarse time bucket so a long stall doesn't
        spam an event every watchdog tick."""
        now = time.monotonic()
        warn_window = max(int(HANDLER_SLOW_WARN_S), 1)
        for r in list(self._inflight.values()):
            age = int(now - r["started_at"])
            if age >= HANDLER_SLOW_WARN_S:
                await self._emit_indicator(
                    "agent.slow",
                    {"agent": r["agent"], "handler": r["handler"], "event_id": r["event_id"], "age_s": age},
                    dedup=f"slow-{r['event_id']}-{age // warn_window}",  # ~once per warn-window
                )
        if len(self._inflight) < self.max_concurrent_handlers:
            return  # a slot is free → by definition nothing is held up
        try:
            async with self.pool.acquire() as conn:
                backlog = (
                    await conn.fetchval(
                        "SELECT count(*) FROM events WHERE status = 'pending' "
                        "AND emitted_at < now() - interval '2 minutes'"
                    )
                    or 0
                )
        except Exception:  # noqa: BLE001 — a probe failure must not kill the watchdog
            log.exception("saturation backlog probe failed")
            return
        if backlog >= SATURATION_BACKLOG:
            held_by = sorted({r["agent"] for r in self._inflight.values()})
            await self._emit_indicator(
                "dispatch.saturated",
                {
                    "in_flight": len(self._inflight),
                    "max": self.max_concurrent_handlers,
                    "backlog": int(backlog),
                    "held_by": held_by,
                },
                dedup=f"saturated-{int(now // warn_window)}",
            )

    async def _detect_broken_agents(self, conn) -> None:
        """An agent whose recent runs ALL failed (>= BROKEN_AGENT_MIN_RUNS, zero
        completed in the last hour) is broken — it keeps consuming its trigger
        events and producing nothing, silently flatlining its slice of the loop
        while the rest of the lab looks fine. Emit agent.broken (deduped per agent
        per hour) naming it + a sample error so it surfaces instead of hiding."""
        rows = await conn.fetch(
            """
            SELECT agent_name,
                   count(*) FILTER (WHERE status = 'failed')    AS failed,
                   count(*) FILTER (WHERE status = 'completed') AS completed,
                   max(left(error, 200))                        AS sample
            FROM agent_runs
            WHERE started_at > now() - interval '1 hour'
            GROUP BY agent_name
            HAVING count(*) FILTER (WHERE status = 'failed') >= $1
               AND count(*) FILTER (WHERE status = 'completed') = 0
            """,
            BROKEN_AGENT_MIN_RUNS,
        )
        hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        for r in rows:
            await self._emit_indicator(
                "agent.broken",
                {"agent": r["agent_name"], "failed": r["failed"], "window": "1h", "sample_error": r["sample"]},
                dedup=f"broken-{r['agent_name']}-{hour}",
            )

    # -- Closure guard: detection ---------------------------------------

    async def _detect_unclosed_events(self, conn) -> None:
        """Universal event-wiring guard: any event type that lands `no_handler`
        and is NOT on CLOSURE_EXEMPT_EVENTS is a loop-closing event being silently
        dropped (a handler unregistered in the current run mode, a typo'd emit, a
        new event type nobody wired). This converts every such dead-end — current
        OR future — from silent to a loud loop.unclosed indicator."""
        rows = await conn.fetch(
            "SELECT event_type, count(*) AS n, max(emitted_at) AS last FROM events "
            "WHERE status = 'suppressed' AND suppression_reason = 'no_handler' "
            "AND emitted_at > now() - ($1||' days')::interval "
            "GROUP BY event_type HAVING count(*) >= $2 ORDER BY 2 DESC",
            str(CLOSURE_LOOKBACK_DAYS),
            UNCLOSED_EVENT_MIN,
        )
        hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        for r in rows:
            et = r["event_type"]
            if et in CLOSURE_EXEMPT_EVENTS:
                continue
            await self._emit_indicator(
                "loop.unclosed",
                {"kind": "unhandled_event", "event_type": et, "count": r["n"], "window_days": CLOSURE_LOOKBACK_DAYS},
                dedup=f"unclosed-event-{et}-{hour}",
            )

    async def _detect_stuck_directions(self, conn) -> None:
        """A direction that is approved + active but has ≥1 task, ALL terminal, and
        no OPEN task is committed work that has gone quiet mid-loop — it blocks new
        deliberation while producing nothing. The research-closure ladder resolves
        the thin_corpus ones; this surfaces any residual (e.g. a grounded direction
        nothing ever advanced) so it can't hide. Deduped per claim per day."""
        rows = await conn.fetch(
            "SELECT c.id, c.status FROM claims c "
            "JOIN direction_gate dg ON dg.claim_id = c.id AND dg.status = 'approved' "
            "WHERE c.claim_kind = 'direction' AND c.status = ANY($1) "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id AND t.status IN ('pending','running'))",
            list(ACTIVE_CLAIM),
        )
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        for r in rows:
            await self._emit_indicator(
                "loop.unclosed",
                {"kind": "direction_stalled", "claim_id": r["id"], "status": r["status"]},
                dedup=f"unclosed-dir-{r['id']}-{day}",
            )

    # -- Closure guard: auto-close (research ladder) --------------------

    async def _emit_targeted_sweep(self, conn, claim_id: int, topics: list[str], dedup: str) -> None:
        """Fire ONE scout sweep aimed at a direction's thin topics (library.sweep_requested
        → run_discovery_sweep → web/arXiv/GitHub/OpenML scouts). claim_id is carried so the
        ladder can tell 'we already scouted this direction' on a later tick."""
        await conn.execute(
            "INSERT INTO events (event_type, target_type, target_id, payload, dedup_key) "
            "VALUES ('library.sweep_requested', 'ingest_source', $1, $2::jsonb, $3) "
            "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING",
            claim_id,
            json.dumps(
                {
                    "claim_id": claim_id,
                    "topics": topics,
                    "sort": "relevance",  # targeted niche search → relevance, not newest arXiv-wide
                    "reason": "closure: scout before declaring a gap",
                }
            ),
            dedup,
        )

    async def _requeue_direction(self, conn, claim_id: int, statement: str, stage: str) -> None:
        """Insert a fresh pending research task for a direction (fires trg_emit_task_created
        → task.created → researcher re-attempts against the now-richer corpus). The closure
        stage rides on the payload so a later tick knows where on the ladder we are."""
        await conn.execute(
            "INSERT INTO tasks (department, task_type, description, payload, priority, status, claim_id) "
            "VALUES ('research', 'survey', $1, $2::jsonb, 8, 'pending', $3)",
            f"Re-attempt ({stage}) after corpus growth: {(statement or '')[:120]}",
            json.dumps({"from": "closure", "closure": {"stage": stage, "of_claim": claim_id}}),
            claim_id,
        )

    async def _declare_gap(self, claim_id: int, reason: str) -> None:
        """Retire a direction that genuinely can't progress (corpus exhausted AFTER a targeted
        scout) → frees the approved-direction budget slot for the next direction. Without a state
        client (e.g. tests), falls back to a loop.unclosed indicator. Best-effort — a failure here
        must not kill the watchdog."""
        state = getattr(self, "state", None)
        if state is None:
            await self._emit_indicator(
                "loop.unclosed",
                {"kind": "research_gap", "claim_id": claim_id, "note": reason},
                dedup=f"gap-{claim_id}",
            )
            return
        try:
            await state.invalidate_claim(claim_id, reason=reason, verdict_id=None)
            log.info("closure: direction %s retired as a research gap — %s", claim_id, reason)
        except Exception:  # noqa: BLE001 — a gap-declaration failure must not kill the watchdog
            log.exception("closure: failed to retire direction %s", claim_id)

    async def _advance_research_closure(self, conn) -> None:
        """Auto-close the thin_corpus research loop, one bounded step per eligible tick:

            thin_corpus → acquire delivered NEW corpus?  → re-queue (acquire_retry)
                        → else no scout yet               → ONE targeted scout sweep
                        → else scout settled              → re-queue (scouted_retry)
                        → else still thin after scouting  → genuine gap → invalidate

        Idempotent: a re-queue creates an OPEN task, which removes the direction from
        the candidate set until it completes, so no step double-fires. Only runs while
        the researcher is active (else the lab isn't researching — nothing to close)."""
        if await get_agent_mode(self.pool, "researcher") not in {"advisory", "active"}:
            return
        candidates = await conn.fetch(
            "SELECT c.id, c.statement FROM claims c "
            "JOIN direction_gate dg ON dg.claim_id = c.id AND dg.status = 'approved' "
            "WHERE c.claim_kind = 'direction' AND c.status = ANY($1) "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.claim_id = c.id AND t.status IN ('pending','running'))",
            list(ACTIVE_CLAIM),
        )
        for c in candidates:
            cid = c["id"]
            latest = await conn.fetchrow(
                "SELECT completed_at, result->>'disposition' AS disp, payload->'closure'->>'stage' AS stage "
                "FROM tasks WHERE claim_id = $1 ORDER BY id DESC LIMIT 1",
                cid,
            )
            if latest is None:
                continue
            if latest["disp"] == "corpus_exhausted":
                # Researcher confirmed a genuine gap (only reachable AFTER a scout now, via the
                # refine_disposition fix). With a 1-direction budget, RETIRING it is what frees the
                # slot for the next direction — else a concluded dead-end holds the only slot forever
                # and the whole research pipeline halts behind it.
                await self._declare_gap(cid, "research gap: corpus exhausted after acquire + targeted scout")
                continue
            if latest["disp"] != "thin_corpus":
                continue  # grounded/other → the detector surfaces it; the ladder skips
            # An acquire still being adjudicated? Wait for the batch to resolve.
            if await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type='acquire.requested' AND status='pending' "
                "AND payload->>'claim_id' = $1",
                str(cid),
            ):
                continue
            since = latest["completed_at"]
            fulfilled_new = await conn.fetchval(
                "SELECT count(*) FROM events WHERE event_type='acquire.fulfilled' "
                "AND payload->>'claim_id' = $1 AND payload->>'status' = 'fulfilled' AND emitted_at > $2",
                str(cid),
                since,
            )
            stage = latest["stage"]
            # (2a) acquire delivered new corpus and we haven't retried on it yet → retry.
            if fulfilled_new and stage not in ("acquire_retry", "scouted_retry"):
                await self._requeue_direction(conn, cid, c["statement"], "acquire_retry")
                log.info("closure: re-queued direction %s (acquire delivered %d new source(s))", cid, fulfilled_new)
                continue
            scout_at = await conn.fetchval(
                "SELECT max(emitted_at) FROM events WHERE event_type='library.sweep_requested' "
                "AND payload->>'claim_id' = $1",
                str(cid),
            )
            # (2b) never scouted → fire ONE targeted scout sweep on this direction's thin topics.
            if scout_at is None:
                topics = [
                    r["q"]
                    for r in await conn.fetch(
                        "SELECT DISTINCT payload->>'query' AS q FROM events WHERE event_type='acquire.requested' "
                        "AND payload->>'claim_id' = $1 AND payload->>'query' IS NOT NULL LIMIT 6",
                        str(cid),
                    )
                ] or [(c["statement"] or "")[:120]]
                await self._emit_targeted_sweep(conn, cid, topics, f"closure-scout-{cid}")
                log.info("closure: fired targeted scout sweep for direction %s (topics=%d)", cid, len(topics))
                continue
            now = await conn.fetchval("SELECT now()")
            if (now - scout_at).total_seconds() < SCOUT_SETTLE_MIN * 60:
                continue  # let the scout's sources discover + ingest
            # (3) scout settled, not yet re-attempted post-scout → scouted retry.
            if stage != "scouted_retry":
                await self._requeue_direction(conn, cid, c["statement"], "scouted_retry")
                log.info("closure: re-queued direction %s (scouted_retry — scout has settled)", cid)
                continue
            # (4) STILL thin after acquire + a targeted scout → a genuine research gap. Pivot.
            await self._declare_gap(cid, "research gap: corpus still thin after acquire + targeted scout")

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
                    await self._detect_broken_agents(conn)
                    await self._detect_unclosed_events(conn)
                    await self._advance_research_closure(conn)  # auto-close the research ladder
                    await self._detect_stuck_directions(conn)  # flag any residual the ladder didn't clear
                await self._detect_stalls()
                await self._reconcile_lessons_if_due()
                await self._sweep_library_if_due()
            except Exception:
                log.exception("watchdog sweep failed")
            await asyncio.sleep(300)

    @staticmethod
    def _ariadne_active() -> bool:
        """The research workflow — Ariadne, the PI — is running when NOT in
        KNOWLEDGE_CORE_ONLY mode. While she's dark, the lab base-builds the
        Library continuously (the discovery pump); once she's active, discovery
        relaxes to a gentle agenda-tracking cadence."""
        return os.environ.get("KNOWLEDGE_CORE_ONLY", "").lower() not in {"1", "true", "on", "yes"}

    async def _emit_sweep(self, dedup_key: str) -> None:
        """Emit one `library.sweep_requested` (the Mimir handler turns it into
        scout runs -> source.discovered). Shared by the agenda sweep + the pump."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, payload, dedup_key)
                VALUES ('library.sweep_requested', 'ingest_source', '{}'::jsonb, 'sweep-' || $1)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                dedup_key,
            )

    async def _intake_backlog(self) -> int:
        """Work still in flight in the intake pipeline: a queued sweep plus
        discovered-not-staged and staged-not-certified sources (pending
        source.discovered / document.parsed events). The pump tops up when this
        runs low so the pipeline is always working and never sits idle."""
        try:
            async with self.pool.acquire() as conn:
                return (
                    await conn.fetchval(
                        "SELECT count(*) FROM events WHERE status = 'pending' AND event_type IN "
                        "('library.sweep_requested', 'source.discovered', 'document.parsed')"
                    )
                    or 0
                )
        except Exception:  # noqa: BLE001 — a backlog probe failure must not kill the pump
            log.exception("pump: intake backlog probe failed")
            return 0

    async def _sweep_library_if_due(self) -> None:
        """Steady-state AGENDA discovery: once Ariadne (the PI) is active, top up
        the Library on a gentle cadence (LIBRARIAN_SWEEP_HOURS, default 6h)
        tracking her claims. While she's dark, base-building runs CONTINUOUSLY via
        the discovery pump, so this no-ops in that mode. An interval is the right
        tool here — an agenda top-up is a slow background refresh, not the main
        intake driver."""
        if os.environ.get("MIMIR_LOOP", "").lower() not in {"v1", "on"}:
            return
        if not self._ariadne_active():
            return  # the continuous discovery pump owns aggressive base-building
        try:
            hours = float(os.environ.get("LIBRARIAN_SWEEP_HOURS", "6"))
        except ValueError:
            hours = 6.0
        now = datetime.now(UTC)
        if self._last_sweep_tick is not None and (now - self._last_sweep_tick).total_seconds() < hours * 3600:
            return
        self._last_sweep_tick = now
        await self._emit_sweep(str(int(now.timestamp() // (hours * 3600))))
        log.info("library: emitted agenda sweep (every %sh)", hours)

    async def _discovery_pump_loop(self) -> None:
        """Continuous library-intake pump — the base-building driver while Ariadne
        (the PI) is dark. CONDITION-driven, not interval-driven: whenever the
        intake backlog runs below a low-water mark it fires the next discovery
        slice, so the pipeline is always working and never idles between ticks.
        Bounded by backpressure (it waits while the backlog is healthy) plus a
        short min-gap covering a sweep's fetch latency so requests don't stack.
        Idle while the loop is off or once Ariadne is active (the agenda sweep
        takes over then)."""
        low_water = int(os.environ.get("LIBRARY_PUMP_LOW_WATER", "40"))
        check_s = float(os.environ.get("LIBRARY_PUMP_CHECK_SECONDS", "10"))
        min_gap_s = float(os.environ.get("LIBRARY_PUMP_MIN_GAP_SECONDS", "60"))
        while self._running:
            try:
                loop_on = os.environ.get("MIMIR_LOOP", "").lower() in {"v1", "on"}
                if loop_on and not self._ariadne_active():
                    now = datetime.now(UTC)
                    gap_ok = self._last_pump_emit is None or (now - self._last_pump_emit).total_seconds() >= min_gap_s
                    if gap_ok and await self._intake_backlog() < low_water:
                        self._last_pump_emit = now
                        await self._emit_sweep(f"pump-{int(now.timestamp())}")
                        log.info("pump: intake backlog low — fired next discovery slice")
            except Exception:  # noqa: BLE001 — the pump must never die
                log.exception("discovery pump iteration failed")
            await asyncio.sleep(check_s)

    async def _reconcile_lessons_if_due(self) -> None:
        """
        Hinge B of the learning loop: hourly, promote/retire lessons from their
        application outcomes and decay stale probationary ones. Both are no-ops
        until outcomes exist (hinge A writes them), so this is always safe.

        Emits `lessons.reconciled` carrying the changed ids so Bench/Debug can
        show the lab's notebook updating.
        """
        lessons = getattr(self, "lessons", None)
        if lessons is None:
            return
        now = datetime.now(UTC)
        if self._last_lessons_tick is not None and (now - self._last_lessons_tick).total_seconds() < 3600:
            return
        self._last_lessons_tick = now
        # Hinge A (default off): judge applied-lesson outcomes so reconcile has
        # something to act on. Inert until LESSON_JUDGE=on + shadow-validated.
        if os.environ.get("LESSON_JUDGE") == "on" and getattr(self, "curator", None) and getattr(self, "router", None):
            try:
                from agents.reflection.handler import judge_pending_lesson_applications

                await judge_pending_lesson_applications(self)
            except Exception:
                log.exception("lesson application judging failed")
        try:
            reconciled = await lessons.reconcile()
            decayed = await lessons.decay()
        except Exception:
            log.exception("lessons reconcile/decay failed")
            return
        if not reconciled and not decayed:
            return
        log.info("lessons: %d reconciled, %d decayed", len(reconciled), len(decayed))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, payload, dedup_key)
                VALUES ('lessons.reconciled', 'lessons', $1::jsonb, 'reconcile-' || $2)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                json.dumps(
                    {
                        "reconciled": [dict(r) for r in reconciled],
                        "decayed": [dict(r) for r in decayed],
                    },
                    default=str,
                ),
                now.strftime("%Y-%m-%dT%H"),
            )

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
        # DISABLED — Stage 0 (market-PI neutralized). This watchdog used to emit
        # `phase.budget_exceeded` on a 1.5× phase overrun, which autonomously drove
        # `phase.transition_proposed` → a committed market charter written into
        # company_state → every agent's prompt swapped to that charter. A research lab
        # (Ariadne is the research PI) must have no market-lifecycle autopilot, so the
        # trigger is removed. The handlers are also unregistered (harness/main.py) and
        # the charter injection is disabled (curator._constitution_layer) — defence in depth.
        return

    async def _refresh_slop_view(self, conn) -> None:
        # Plain REFRESH (not CONCURRENTLY): migration 008 recreated this matview as
        # slop_rate_by_claim WITHOUT the unique index that CONCURRENTLY requires.
        # The view is tiny (one row per claim), so the brief lock is negligible.
        await conn.execute("REFRESH MATERIALIZED VIEW slop_rate_by_claim")
