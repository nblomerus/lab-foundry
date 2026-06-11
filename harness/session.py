"""
Agent session — groups multiple agent_runs into one multi-step execution.

A Session is the framework-level container around a single handler invocation.
The researcher v2 loop is the first user: one Session per claimed research task,
containing one agent_run per loop step (plan_inquiry, extract_evidence per page,
synthesize, gap_check, …).

Created in `dispatch.py` at the same point `triggered_by_event_id` is bound,
then threaded into the handler call via `dispatcher.session`. Handlers don't
need to know about sessions directly — they pass `session=` through to
`router.invoke`, which writes session linkage onto the agent_runs row and
emits `step.started` / `step.completed` / `step.failed` events.

The Session's `mode` flag (`live` | `replay`) is read by replay-aware code
paths (Router cost tracking, Dispatcher cooldowns / state mutations) so a
bench-style replay doesn't pollute the live loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

import asyncpg

log = logging.getLogger(__name__)


Mode = Literal["live", "replay"]


@dataclass
class Session:
    """Lifecycle handle for a multi-step handler invocation.

    Not a context manager — there is no resource to release. Call `start()`
    once at the top of the handler (or in dispatch.py), then `finish(status)`
    at the end. `step_order` and `last_step_id` are bookkeeping the Router
    reads and updates per `router.invoke(session=...)` call.
    """

    handler_name: str
    triggered_by_event_id: int | None = None
    mode: Mode = "live"
    # Populated by start(); 0 until then.
    id: int = 0
    # Bookkeeping: incremented by Router before each step insert.
    step_order: int = 0
    # The most recently inserted step's agent_runs.id, used as the default
    # parent_step_id for the next step (linear chains). Multi-parent steps
    # (e.g. extract_evidence fan-out) override this explicitly.
    last_step_id: int | None = None
    # The agent_runs.id of the most recent _chain_complete call on this session
    # (the non-Router LLM path). Lets that path's caller credit the run (e.g.
    # Ariadne's reflection crediting a re-derived lesson) without threading ids.
    last_run_id: int | None = None

    # Held for emit_event(). Not on dataclass equality / repr.
    _pool: asyncpg.Pool | None = field(default=None, repr=False, compare=False)

    async def start(self, pool: asyncpg.Pool) -> None:
        """Insert the agent_sessions row and emit session.started."""
        self._pool = pool
        async with pool.acquire() as conn:
            self.id = await conn.fetchval(
                """
                INSERT INTO agent_sessions (
                    handler_name, triggered_by_event_id, status, mode
                )
                VALUES ($1, $2, 'running', $3)
                RETURNING id
                """,
                self.handler_name,
                self.triggered_by_event_id,
                self.mode,
            )
        await self.emit_event(
            event_type="session.started",
            payload={"handler": self.handler_name, "mode": self.mode},
        )

    async def finish(self, status: str, error: str | None = None) -> None:
        """Mark the session completed/failed and emit session.completed/failed.

        Idempotent-ish: only updates if we have an id. Safe to call from
        finally blocks where start() may not have run.
        """
        if not self.id or self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_sessions
                SET status = $1, completed_at = NOW(), error = $2
                WHERE id = $3
                """,
                status,
                (error or None) and error[:500],
                self.id,
            )
        await self.emit_event(
            event_type=f"session.{status}",
            payload={
                "handler": self.handler_name,
                "step_count": self.step_order,
                "error": error[:200] if error else None,
            },
        )

    async def emit_event(
        self,
        *,
        event_type: str,
        payload: dict,
        target_type: str | None = None,
        target_id: int | None = None,
        emitted_by_run_id: int | None = None,
    ) -> None:
        """Insert an event row tagged with this session_id.

        Replay sessions also emit events so the trace UI can replay-mode them
        in real time, but they're tagged with session.mode='replay' so
        downstream dispatchers can suppress side effects. (Cooldowns and
        cross-handler causality are gated elsewhere — this just writes the row.)
        """
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (
                    event_type, target_type, target_id, payload,
                    emitted_by_run_id, session_id
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                """,
                event_type,
                target_type,
                target_id,
                json.dumps(payload),
                emitted_by_run_id,
                self.id or None,
            )

    def next_step_order(self) -> int:
        """Bump and return the next step_order. Called by Router on each invoke."""
        self.step_order += 1
        return self.step_order
