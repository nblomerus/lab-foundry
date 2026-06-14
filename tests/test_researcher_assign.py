"""Unit tests for agents/researcher/assign.py — the direction→researcher ownership policy.

Specialty-match (keyword) tie-broken by least-loaded; assign_direction is idempotent and propagates
the owner to the direction's tasks; backfill catches approved-but-unowned directions. ScriptedPool-mocked.
"""

from __future__ import annotations

import pytest

from agents.researcher import assign
from agents.researcher.identity import Researcher
from tests._helpers import ScriptedPool

pytestmark = pytest.mark.asyncio


def _r(rid, name, specialty):
    return Researcher(id=rid, name=name, persona="", specialty=specialty, model=None, status="active")


def _row(rid, name, specialty):
    return {"id": rid, "name": name, "persona": "", "specialty": specialty, "model": None, "status": "active"}


_SPECS = [
    (1, "Daedalus", "systems-optimization"),
    (2, "Hypatia", "statistics-calibration"),
    (3, "Heron", "llm-retrieval-eval"),
]
_ROSTER = [_r(*s) for s in _SPECS]
_ROSTER_ROWS = [_row(*s) for s in _SPECS]


async def test_specialty_score_counts_keyword_hits():
    assert assign._specialty_score("conformal calibration of uncertainty", "statistics-calibration") >= 2
    assert assign._specialty_score("retrieval reranking with an llm", "llm-retrieval-eval") >= 2
    assert assign._specialty_score("nothing relevant here", "statistics-calibration") == 0


async def test_pick_prefers_specialty_match():
    chosen = assign._pick(_ROSTER, "conformal calibration uncertainty estimator coverage", {})
    assert chosen.name == "Hypatia"


async def test_pick_ties_broken_by_least_loaded():
    chosen = assign._pick(_ROSTER, "xyzzy plugh nothing matches", {1: 5, 2: 0, 3: 5})
    assert chosen.name == "Hypatia"  # no keyword hit → least loaded wins


async def test_pick_empty_roster_returns_none():
    assert assign._pick([], "anything", {}) is None


async def test_assign_direction_idempotent_when_already_owned():
    pool = ScriptedPool(
        [
            ("researcher_id FROM claims WHERE id", 2),
            ("FROM researchers WHERE id", [_row(2, "Hypatia", "statistics-calibration")]),
        ]
    )
    r = await assign.assign_direction(pool, 5)
    assert r.name == "Hypatia"


async def test_assign_direction_picks_writes_and_returns():
    pool = ScriptedPool(
        [
            ("researcher_id FROM claims WHERE id", None),  # not yet owned
            ("status = 'active'", _ROSTER_ROWS),  # roster
            ("statement FROM claims WHERE id", "conformal calibration uncertainty estimator"),
            ("count(*) AS n FROM claims", []),  # load = empty
        ]
    )
    r = await assign.assign_direction(pool, 7)
    assert r.name == "Hypatia"
    # it wrote the owner onto the claim and propagated to tasks
    execs = [c for c in pool.calls if c[0] == "execute"]
    assert any("UPDATE claims SET researcher_id" in c[1] for c in execs)
    assert any("UPDATE tasks SET researcher_id" in c[1] for c in execs)


async def test_assign_direction_no_roster_returns_none():
    pool = ScriptedPool(
        [
            ("researcher_id FROM claims WHERE id", None),
            ("status = 'active'", []),  # empty roster
            ("statement FROM claims WHERE id", "anything"),
            ("count(*) AS n FROM claims", []),
        ]
    )
    assert await assign.assign_direction(pool, 7) is None


async def test_backfill_unassigned_empty_is_zero():
    pool = ScriptedPool([("JOIN direction_gate g", [])])
    assert await assign.backfill_unassigned(pool) == 0


async def test_backfill_unassigned_assigns_each():
    pool = ScriptedPool(
        [
            ("JOIN direction_gate g", [{"id": 7}]),  # one approved-but-unowned direction
            ("researcher_id FROM claims WHERE id", None),
            ("status = 'active'", _ROSTER_ROWS),
            ("statement FROM claims WHERE id", "calibration uncertainty"),
            ("count(*) AS n FROM claims", []),
        ]
    )
    assert await assign.backfill_unassigned(pool) == 1
