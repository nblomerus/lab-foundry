"""
WebSocket event stream. A single Postgres LISTEN connection fans out NOTIFY
messages to all connected clients, plus enriches each event with the
corresponding row data so the frontend can update without re-fetching.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)
router = APIRouter()


class StreamHub:
    """Fans Postgres NOTIFY messages out to all connected WebSockets."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.listener_conn: asyncpg.Connection | None = None
        self._pool: asyncpg.Pool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._loop = asyncio.get_running_loop()
        self.listener_conn = await pool.acquire()
        await self.listener_conn.add_listener("events", self._on_notify)
        log.info("StreamHub listening on Postgres 'events' channel")

    async def stop(self) -> None:
        if self.listener_conn and self._pool:
            with contextlib.suppress(Exception):
                await self.listener_conn.remove_listener("events", self._on_notify)
            await self._pool.release(self.listener_conn)
            self.listener_conn = None

    def _on_notify(self, conn, pid, channel, payload):
        try:
            data = json.loads(payload)
        except Exception:
            return
        if self._loop:
            self._loop.create_task(self._broadcast_event(data["id"]))

    async def _broadcast_event(self, event_id: int) -> None:
        if not self._pool or not self.clients:
            return
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM events WHERE id = $1",
                event_id,
            )
        if row is None:
            return

        # Enrich the event with the row that changed so clients can update in place.
        enrichment = await self._enrich(row)

        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload) if payload else {}
            except Exception:
                payload = {}
        msg = {
            "type": "event",
            "event": {
                "id": row["id"],
                "event_type": row["event_type"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "session_id": row["session_id"],
                "payload": dict(payload) if payload else {},
                "status": row["status"],
                "emitted_at": row["emitted_at"].isoformat(),
            },
            **enrichment,
        }
        await self._fanout(msg)

    async def _enrich(self, event_row: asyncpg.Record) -> dict[str, Any]:
        """For events that change a known entity, attach its updated state.

        Skips enrichment entirely for step.* / session.* events: their payload
        already carries everything the trace UI needs (step_name, model,
        status, error, session_id) and the per-step volume would otherwise
        run two pool queries per event × N clients, exhausting asyncpg.
        """
        if self._pool is None:
            return {}
        target_type = event_row["target_type"]
        target_id = event_row["target_id"]
        event_type = event_row["event_type"]

        if event_type.startswith(("step.", "session.")):
            return {"session_id": event_row.get("session_id")}

        try:
            async with self._pool.acquire() as conn:
                if target_type == "thesis" and target_id:
                    t = await conn.fetchrow(
                        "SELECT * FROM claims WHERE id = $1",
                        target_id,
                    )
                    if t:
                        return {"thesis": _serialize(t)}
                elif target_type == "task" and target_id:
                    if event_type == "task.completed":
                        t = await conn.fetchrow(
                            "SELECT * FROM tasks WHERE id = $1",
                            target_id,
                        )
                        return {"task": _serialize(t)} if t else {}
                elif event_type == "finding.high_signal" and target_id:
                    fid = (event_row["payload"] or {}).get("finding_id")
                    if fid:
                        f = await conn.fetchrow(
                            "SELECT * FROM findings WHERE id = $1",
                            fid,
                        )
                        if f:
                            return {"finding": _serialize(f)}
                elif target_type == "phase":
                    s = await conn.fetchrow(
                        "SELECT * FROM company_state WHERE id = 1",
                    )
                    if s:
                        return {"company_state": _serialize(s)}
        except Exception:
            log.exception("event enrichment failed for event %s", event_row["id"])
        return {}

    async def _fanout(self, msg: dict) -> None:
        dead: list[WebSocket] = []
        for client in list(self.clients):
            try:
                await client.send_json(msg)
            except Exception:
                dead.append(client)
        for d in dead:
            self.clients.discard(d)


def _serialize(row: asyncpg.Record) -> dict:
    """Convert an asyncpg Record into a JSON-safe dict."""
    out: dict = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime) or hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            try:
                json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = str(v)
    return out


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    hub: StreamHub = websocket.app.state.stream_hub
    await websocket.accept()
    hub.clients.add(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            # We don't expect inbound messages; this keeps the connection open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        hub.clients.discard(websocket)
