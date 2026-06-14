"""Unit tests for agents/researcher/identity.py — the researcher roster identity helpers.

load_researcher / researcher_for_task / active_roster read the roster (migration 022); system_prompt
composes the full-stack persona. Everything is mocked via ScriptedPool — no real Postgres.
"""

from __future__ import annotations

import pytest

from agents.researcher import identity
from agents.researcher.identity import Researcher
from tests._helpers import ScriptedPool

pytestmark = pytest.mark.asyncio

_ROW = {
    "id": 2,
    "name": "Hypatia",
    "persona": "A mathematician's eye for calibration.",
    "specialty": "statistics-calibration",
    "model": None,
    "status": "active",
}


def _task(researcher_id=None, claim_id=None):
    return type("T", (), {"researcher_id": researcher_id, "claim_id": claim_id})()


async def test_load_researcher_none_id_returns_none():
    assert await identity.load_researcher(ScriptedPool(), None) is None


async def test_load_researcher_fetches_and_maps_row():
    pool = ScriptedPool([("FROM researchers WHERE id", [_ROW])])
    r = await identity.load_researcher(pool, 2)
    assert isinstance(r, Researcher)
    assert (r.id, r.name, r.specialty, r.status) == (2, "Hypatia", "statistics-calibration", "active")


async def test_load_researcher_missing_returns_none():
    pool = ScriptedPool([("FROM researchers WHERE id", [])])
    assert await identity.load_researcher(pool, 99) is None


async def test_active_roster_orders_and_maps():
    pool = ScriptedPool([("WHERE status = 'active'", [_ROW, {**_ROW, "id": 1, "name": "Daedalus"}])])
    roster = await identity.active_roster(pool)
    assert [r.name for r in roster] == ["Hypatia", "Daedalus"]


async def test_researcher_for_task_uses_task_owner():
    pool = ScriptedPool([("FROM researchers WHERE id", [_ROW])])
    r = await identity.researcher_for_task(pool, _task(researcher_id=2, claim_id=5))
    assert r.name == "Hypatia"


async def test_researcher_for_task_falls_back_to_claim_owner():
    pool = ScriptedPool(
        [
            ("researcher_id FROM claims WHERE id", 2),  # claim owns it
            ("FROM researchers WHERE id", [_ROW]),
        ]
    )
    r = await identity.researcher_for_task(pool, _task(researcher_id=None, claim_id=5))
    assert r.name == "Hypatia"


async def test_system_prompt_generic_when_none():
    p = identity.system_prompt(None)
    assert "full-stack" in p and "researcher" in p.lower()


async def test_system_prompt_personalised():
    p = identity.system_prompt(Researcher(**_ROW))
    assert "Hypatia" in p and "statistics-calibration" in p
