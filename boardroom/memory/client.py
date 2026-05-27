"""
Zep memory client.

Owns the boardroom's narrative episodic record. Postgres holds *what is true now*;
Zep holds *how we got here*. They're joined by memory_pointers rows when a
specific entity's story needs to be retrievable by id.

Sessions used in boardroom:
    theses-lifecycle    one message per thesis event
    phase-transitions   one message per phase change
    ceo-deliberations   CEO's working-out for non-trivial decisions
    dissent             adversary verdicts + auditor slop flags
    charter             written at commitment, immutable

The Graphiti graph extracts entities and relationships automatically;
recall_graph queries that graph.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


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
        """
        if session_id in self._ensured_sessions:
            return
        try:
            await self._client.thread.create(
                thread_id=session_id,
                user_id=self.USER_ID,
            )
        except Exception:
            pass  # already exists
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
        Returns the Zep message uuid (or '' if unavailable).

        v3 messages carry a constrained `role` enum plus a free-form `name`;
        boardroom's logical role ("ceo", "adversary", …) goes in `name`, and
        everything the company writes is assistant-authored.
        """
        from zep_cloud.types import Message

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
                created_at=getattr(m, "created_at", None) or datetime.now(timezone.utc),
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
                created_at=getattr(ep, "created_at", None) or datetime.now(timezone.utc),
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
