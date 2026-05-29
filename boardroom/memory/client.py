"""
Zep memory client.

Owns the boardroom's narrative episodic record. Postgres holds *what is true now*;
Zep holds *how we got here*. They're joined by memory_pointers rows when a
specific entity's story needs to be retrievable by id.

Sessions used in boardroom:
    claims-lifecycle    one message per claim event
    phase-transitions   one message per phase change
    pi-deliberations   PI's working-out for non-trivial decisions
    dissent             critic verdicts + evaluation slop flags
    charter             written at commitment, immutable

The Graphiti graph extracts entities and relationships automatically;
recall_graph queries that graph.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


def _coerce_dt(value) -> datetime:
    """Zep v3 returns created_at as an ISO string; downstream code (the
    curator's recall formatting) expects a real datetime. Normalize both."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@dataclass
class RecalledMessage:
    """Decoupled from Zep types so the rest of the harness can swap providers."""
    content: str
    created_at: datetime
    role_type: str
    uuid: str
    relevance: Optional[float] = None  # set on semantic recall only


class ZepClient:
    """Thin async wrapper over zep-cloud."""

    USER_ID = "boardroom"   # singleton — the company itself

    def __init__(self, client):
        self._client = client
        self._ensured_sessions: set[str] = set()
        # Per-session lock so concurrent handlers all trying to ensure the
        # same thread (e.g. "dissent" at harness startup) collapse to a
        # single create() call. Without this, N handlers all read
        # session_id not in _ensured_sessions, all fire thread.create()
        # in parallel, and Zep's 5 req/min thread cap rejects most of them
        # — consuming the budget before any real writes happen.
        self._ensuring_locks: dict[str, asyncio.Lock] = {}
        self._ensuring_meta_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "ZepClient":
        from zep_cloud.client import AsyncZep
        api_key = os.environ["ZEP_API_KEY"]
        base_url = os.environ.get("ZEP_BASE_URL")  # set when self-hosting
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return cls(AsyncZep(**kwargs))

    # ---- Sessions -----------------------------------------------------

    async def ping(self) -> None:
        """
        Health check for preflight. Raises if the client is the wrong shape
        (e.g. an API rename like memory->thread) or the service is unreachable.

        A 'not found' from get is a *healthy* response — it means the request
        round-tripped and auth is valid; we only surface connection/auth/shape
        failures.
        """
        for ns in ("thread", "graph", "user"):
            if not hasattr(self._client, ns):
                raise RuntimeError(f"Zep client missing '{ns}' namespace (API drift?)")
        try:
            await self._client.thread.get(thread_id="__boardroom_healthcheck__", lastn=1)
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg or "404" in msg or "does not exist" in msg:
                return  # reachable + authed; the test thread just doesn't exist
            raise

    async def ensure_user(self) -> None:
        """Create the singleton user if it doesn't exist. Idempotent."""
        try:
            await self._client.user.add(user_id=self.USER_ID)
        except Exception:
            pass  # already exists

    async def ensure_session(self, session_id: str) -> None:
        """Create a thread if it doesn't exist. Idempotent. Cached locally.

        Zep Cloud v3 renamed the `memory`/session namespace to `thread`; a
        thread is the v3 equivalent of the old session.

        Single-flight per session: a per-session asyncio.Lock collapses
        concurrent callers (all the handlers narrating to "dissent" right
        after startup) to exactly one create() round-trip. The first caller
        does the work; subsequent callers wait on the lock, then see the
        cache hit and return without a network call.
        """
        if session_id in self._ensured_sessions:
            return
        # Get-or-create the per-session lock under a tiny meta lock so two
        # concurrent first-callers don't each create their own Lock object
        # for the same session and race past it.
        async with self._ensuring_meta_lock:
            lock = self._ensuring_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._ensuring_locks[session_id] = lock
        async with lock:
            # Re-check after acquiring: another waiter may have finished while
            # we were queued, in which case the create is already done.
            if session_id in self._ensured_sessions:
                return
            try:
                await self._client.thread.create(
                    thread_id=session_id,
                    user_id=self.USER_ID,
                )
            except Exception as e:  # noqa: BLE001 — already-exists is the happy case
                # Don't try to distinguish 400-already-exists from 429-rate-limit:
                # Zep echoes rate-limit headers on every response so substring
                # checks on str(e) misfire on the harmless 400s. Cache the
                # outcome regardless — these sessions are long-lived (created
                # weeks ago, persistent in Zep), so a single failed create
                # within this process means "either it already exists, or
                # write_message will log and skip when it tries to use it".
                # write_message is best-effort so neither breaks the harness.
                log.debug("zep ensure_session(%s) error (treated as exists): %s",
                          session_id, e)
            self._ensured_sessions.add(session_id)

    # ---- Writes -------------------------------------------------------

    async def write_message(
        self,
        session_id: str,
        content: str,
        role_type: str = "agent",
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Append a message to a thread. Triggers async graph extraction.
        Returns the Zep message uuid (or '' if unavailable / failure).

        v3 messages carry a constrained `role` enum plus a free-form `name`;
        boardroom's logical role ("pi", "adversary", …) goes in `name`, and
        everything the company writes is assistant-authored.

        Best-effort: a Zep 429 / network blip / API drift must not blow up the
        calling handler. Narrative writes are observational (dissent log,
        decision trail) — if they fail we log and return "". Reads already
        behave this way (`recent`, `recall_episodic`); writes were the only
        side that raised, which surfaced as session-wide handler failures
        during Zep rate-limit windows (the boardroom-harness startup burst
        used to drown the 5 req/min thread cap and take audit_slop_detected
        with it). Callers treat the empty uuid as "not persisted" already.
        """
        from zep_cloud.types import Message

        try:
            await self.ensure_session(session_id)
            result = await self._client.thread.add_messages(
                thread_id=session_id,
                messages=[Message(
                    role="assistant",
                    name=role_type,
                    content=content,
                    metadata=metadata or {},
                )],
            )
        except Exception as e:  # noqa: BLE001 — narrative write is best-effort
            log.warning(
                "zep write_message(%s, role=%s) failed: %s",
                session_id, role_type, e,
            )
            return ""
        uuids = getattr(result, "message_uuids", None) or []
        return uuids[0] if uuids else ""

    # ---- Reads --------------------------------------------------------

    async def recent(self, session_id: str, k: int = 5) -> list[RecalledMessage]:
        """Hot path: last k messages from a thread, chronological order."""
        try:
            result = await self._client.thread.get(thread_id=session_id, lastn=k)
        except Exception:
            return []
        return [
            RecalledMessage(
                content=m.content or "",
                created_at=_coerce_dt(getattr(m, "created_at", None)),
                role_type=getattr(m, "name", None) or getattr(m, "role", None) or "agent",
                uuid=getattr(m, "uuid_", None) or "",
            )
            for m in (getattr(result, "messages", None) or [])
        ]

    async def recall_episodic(
        self,
        session_id: str,
        query: str,
        k: int = 5,
    ) -> list[RecalledMessage]:
        """Cold path: semantic recall over the company's ingested episodes.

        v3 has no per-thread message search; semantic recall runs against the
        knowledge graph. We query episodes (the graph's record of ingested
        messages) for the boardroom user.
        """
        try:
            result = await self._client.graph.search(
                user_id=self.USER_ID,
                query=query,
                limit=k,
                scope="episodes",
            )
        except Exception:
            return []
        out = []
        for ep in (getattr(result, "episodes", None) or []):
            out.append(RecalledMessage(
                content=getattr(ep, "content", None) or "",
                created_at=_coerce_dt(getattr(ep, "created_at", None)),
                role_type=getattr(ep, "role_type", None) or getattr(ep, "role", None) or "agent",
                uuid=getattr(ep, "uuid_", None) or "",
                relevance=getattr(ep, "score", None),
            ))
        return out

    async def recall_graph(
        self,
        query: str,
        entity_type: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Query the Graphiti knowledge graph. Returns raw dicts (graph schema is
        open-ended). Callers usually pre-process into a known shape.
        """
        try:
            kwargs: dict[str, Any] = {
                "user_id": self.USER_ID,
                "query": query,
                "limit": limit,
            }
            if entity_type:
                kwargs["search_filters"] = {"entity_type": entity_type}
            result = await self._client.graph.search(**kwargs)
        except Exception:
            return []
        out = []
        for r in (getattr(result, "edges", None) or []):
            out.append(r.dict() if hasattr(r, "dict") else dict(r))
        return out
