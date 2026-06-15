"""Agent identity registry — the lab's singleton agents as named, persistent identities.

Generalizes the researcher roster (agents/researcher/identity.py, migration 022) to every singleton
agent (migration 024 `agent_identities`): Ariadne, Mimir, Themis (novelty), Metis (planner),
Calliope (synthesis), Mnemosyne (reflection), Aletheia (evaluation), Momus (critic). The curator
resolves an agent's system persona from here via `persona_for`, falling back to the code-level
`SYSTEM_PROMPTS` constant when there is no row (so a missing identity never breaks a prompt).

Keyed by `agent_name` (the same key as agent_modes / recipe.agent). `agent_modes` is the control
dial; this is the persona/name layer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_COLS = "agent_name, name, role, persona, model, status"
_PERSONA_TTL_S = 300.0
_persona_cache: dict[str, tuple[str | None, float]] = {}  # agent_name -> (composed prompt | None, monotonic ts)


@dataclass(frozen=True)
class Identity:
    agent_name: str
    name: str
    role: str
    persona: str
    model: str | None
    status: str


def _row_to_identity(row) -> Identity:
    return Identity(
        agent_name=row["agent_name"],
        name=row["name"],
        role=row["role"] or "",
        persona=row["persona"] or "",
        model=row["model"],
        status=row["status"],
    )


def system_prompt(i: Identity) -> str:
    """Compose an agent's system persona: who it is + its role + its voice."""
    return f"You are {i.name}, the {i.role} of an autonomous AI research lab. {i.persona}".strip()


async def load_identity(pool, agent_name: str) -> Identity | None:
    """Fetch a singleton agent's identity, or None (no row → curator uses the code fallback)."""
    row = await pool.fetchrow(f"SELECT {_COLS} FROM agent_identities WHERE agent_name = $1", agent_name)
    return _row_to_identity(row) if row else None


async def roster(pool) -> list[Identity]:
    """All identities (for ops.identities / the dashboard pantheon)."""
    rows = await pool.fetch(f"SELECT {_COLS} FROM agent_identities ORDER BY agent_name")
    return [_row_to_identity(r) for r in rows]


async def persona_for(pool, agent_name: str) -> str | None:
    """The composed system persona for `agent_name` (cached, TTL), or None if there is no identity row
    or the lookup fails — the curator then falls back to SYSTEM_PROMPTS. Never raises."""
    now = time.monotonic()
    hit = _persona_cache.get(agent_name)
    if hit is not None and now - hit[1] < _PERSONA_TTL_S:
        return hit[0]
    prompt: str | None = None
    try:
        identity = await load_identity(pool, agent_name)
        if identity is not None and identity.persona:
            prompt = system_prompt(identity)
    except Exception:  # noqa: BLE001 — a DB blip must fall back to the code persona, never break the prompt
        return hit[0] if hit is not None else None
    _persona_cache[agent_name] = (prompt, now)
    return prompt
