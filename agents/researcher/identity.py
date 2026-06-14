"""Researcher identity — the lab's full-stack researchers as persistent, named personas.

A researcher is the lab's ML engineer + software engineer + scientist in one: it designs,
implements, runs, and interprets its own experiments. Directions are assigned to a researcher at
approval (agents.researcher.assign), and that researcher OWNS every task and experiment under the
direction — so each experiment_runs row carries a `researcher_id` (migration 022).

This module loads a researcher from the roster and composes its system prompt. The DB stores a lean
bio (`persona`) + an assignment tag (`specialty`); the full-stack voice is templated here so the
prompt stays a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Researcher:
    id: int
    name: str
    persona: str
    specialty: str
    model: str | None
    status: str


_ROSTER_COLS = "id, name, persona, specialty, model, status"


def _row_to_researcher(row) -> Researcher:
    return Researcher(
        id=row["id"],
        name=row["name"],
        persona=row["persona"] or "",
        specialty=row["specialty"] or "",
        model=row["model"],
        status=row["status"],
    )


async def load_researcher(pool, researcher_id: int | None) -> Researcher | None:
    """Fetch a roster member by id, or None (unassigned / missing). `pool` is an asyncpg pool
    (state.pool from the dispatcher, or the raw pool the pace loop holds)."""
    if researcher_id is None:
        return None
    row = await pool.fetchrow(f"SELECT {_ROSTER_COLS} FROM researchers WHERE id = $1", researcher_id)
    return _row_to_researcher(row) if row else None


async def researcher_for_task(pool, task) -> Researcher | None:
    """Resolve the acting researcher for a claimed task: the task's owner, falling back to the
    direction's owner (claim.researcher_id) if the task row predates ownership denormalisation."""
    rid = getattr(task, "researcher_id", None)
    if rid is None and getattr(task, "claim_id", None) is not None:
        rid = await pool.fetchval("SELECT researcher_id FROM claims WHERE id = $1", task.claim_id)
    return await load_researcher(pool, rid)


async def active_roster(pool) -> list[Researcher]:
    """All active researchers (assignment candidates)."""
    rows = await pool.fetch(f"SELECT {_ROSTER_COLS} FROM researchers WHERE status = 'active' ORDER BY id")
    return [_row_to_researcher(r) for r in rows]


def system_prompt(r: Researcher | None) -> str:
    """The full-stack researcher voice, personalised to the acting identity. Falls back to a generic
    full-stack researcher when no identity is resolved (e.g. legacy rows)."""
    if r is None:
        return (
            "You are a researcher at an autonomous AI research lab — a full-stack ML engineer, "
            "software engineer, and scientist in one. You design, implement, run, and interpret your "
            "own experiments, and you report numbers honestly: a null or failed result is data."
        )
    return (
        f"You are {r.name}, a researcher at an autonomous AI research lab — a full-stack ML engineer, "
        f"software engineer, and scientist in one. {r.persona} Your leaning is {r.specialty}, but you "
        f"own every direction assigned to you end-to-end: you design the experiment, write the code, "
        f"run it, and interpret the result yourself. You favour clean, reproducible studies that fit "
        f"the lab's hardware, and you report numbers honestly — a null or failed result is data, not a "
        f"setback, and you never inflate a weak signal into a conclusion."
    )
