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
        """Create a session if it doesn't exist. Idempotent. Cached locally."""
        if session_id in self._ensured_sessions:
            return
        try:
            await self._client.memory.add_session(
                session_id=session_id,
                user_id=self.USER_ID,
            )
        except Exception:
            pass
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
        Append a message to a session. Triggers async Graphiti extraction.
        Returns the Zep message uuid (or '' if unavailable).
        """
        from zep_cloud.types import Message

        await self.ensure_session(session_id)
        result = await self._client.memory.add(
            session_id=session_id,
            messages=[Message(
                role=role_type,
                role_type=role_type,
                content=content,
                metadata=metadata or {},
            )],
        )
        msgs = getattr(result, "messages", None) or []
        return msgs[0].uuid if msgs else ""

    # ---- Reads --------------------------------------------------------

    async def recent(self, session_id: str, k: int = 5) -> list[RecalledMessage]:
        """Hot path: last k messages from a session, chronological order."""
        try:
            result = await self._client.memory.get(session_id=session_id, lastn=k)
        except Exception:
            return []
        return [
            RecalledMessage(
                content=m.content or "",
                created_at=getattr(m, "created_at", None) or datetime.now(timezone.utc),
                role_type=m.role_type or "agent",
                uuid=m.uuid or "",
            )
            for m in (getattr(result, "messages", None) or [])
        ]

    async def recall_episodic(
        self,
        session_id: str,
        query: str,
        k: int = 5,
    ) -> list[RecalledMessage]:
        """Cold path: semantic search over one session."""
        try:
            result = await self._client.memory.search_sessions(
                session_ids=[session_id],
                text=query,
                limit=k,
            )
        except Exception:
            return []
        out = []
        for r in (getattr(result, "results", None) or []):
            m = getattr(r, "message", None)
            if m is None:
                continue
            out.append(RecalledMessage(
                content=m.content or "",
                created_at=getattr(m, "created_at", None) or datetime.now(timezone.utc),
                role_type=m.role_type or "agent",
                uuid=m.uuid or "",
                relevance=getattr(r, "score", None),
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
