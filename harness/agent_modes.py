"""
Per-agent mode dial — readiness Stage 4 + the debug control plane.

mode ∈ {off, shadow, advisory, active}. The dispatcher RUNS an agent's handler only
when its mode is advisory|active; off|shadow are suppressed. An explicit `agent_modes`
row overrides the KNOWLEDGE_CORE_ONLY-derived default (the decoupling the readiness
review asked for: a per-agent seam, not one atomic flag).

`shadow` does NOT run via the dispatcher: there is no structural write-suppression for
arbitrary event handlers yet, so "run read-only" is realized per-agent where it has an
explicit read-only path (Ariadne's ops.ariadne_firstlight). At the dispatcher, shadow ==
paused (safe). Set modes with ops.agent_mode.
"""

from __future__ import annotations

import os
import time

# Modes the dispatcher will actually run. off/shadow are NOT run (paused / read-only).
_RUNNABLE = frozenset({"advisory", "active"})

# Research-loop agents that the legacy KNOWLEDGE_CORE_ONLY flag gated off. Mimir (the
# Library warden) is never gated by that flag; everything else defaults to active.
_RESEARCH = frozenset(
    {"pi", "ariadne", "critic", "planner", "researcher", "evaluation", "novelty", "reviewer", "reflection", "auditor"}
)

_cache: dict[str, tuple[str, float]] = {}
_TTL = 5.0  # seconds — a mode change takes effect within this window


def _knowledge_core_only() -> bool:
    return os.environ.get("KNOWLEDGE_CORE_ONLY", "").lower() in {"1", "on", "true", "yes"}


def _default_mode(agent: str) -> str:
    """Mode when there's no explicit row — preserves legacy behaviour until set."""
    if agent == "mimir":
        return "active"
    if agent in _RESEARCH:
        return "off" if _knowledge_core_only() else "active"
    return "active"


def agent_of(handler) -> str | None:
    """Derive the agent name from a handler's module (agents.<name>.* → <name>).
    Returns None for system handlers (e.g. library.graph.sink) — never gated."""
    parts = (getattr(handler, "__module__", "") or "").split(".")
    return parts[1] if len(parts) >= 2 and parts[0] == "agents" else None


async def get_agent_mode(pool, agent: str) -> str:
    now = time.monotonic()
    hit = _cache.get(agent)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    mode = None
    try:
        async with pool.acquire() as conn:
            mode = await conn.fetchval("SELECT mode FROM agent_modes WHERE agent_name = $1", agent)
    except Exception:  # noqa: BLE001 — table missing / DB blip → fall back to the default
        mode = None
    mode = mode or _default_mode(agent)
    _cache[agent] = (mode, now)
    return mode


def should_run(mode: str) -> bool:
    return mode in _RUNNABLE


async def set_agent_mode(pool, agent: str, mode: str, note: str | None = None) -> None:
    if mode not in {"off", "shadow", "advisory", "active"}:
        raise ValueError(f"invalid mode {mode!r}")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_modes (agent_name, mode, note, updated_at) VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (agent_name) DO UPDATE SET mode = $2, note = $3, updated_at = now()",
            agent,
            mode,
            note,
        )
    _cache.pop(agent, None)
