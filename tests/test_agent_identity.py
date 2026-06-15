"""Agent identity registry (agents/identity.py) + the curator's persona-resolution wiring.

The companion file tests/test_harness_router_curator.py neutralises persona_for and asserts the
SYSTEM_PROMPTS code fallback; THIS file exercises the positive path — a real agent_identities row
flows into the system layer — plus the module's load/compose/roster/cache/error-fallback branches.

No real Postgres: the DB is a ScriptedPool from tests._helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import identity as identity_mod
from agents.identity import Identity, load_identity, persona_for, roster, system_prompt
from harness.curator import Curator
from tests._helpers import ScriptedPool


@pytest.fixture(autouse=True)
def _clear_persona_cache():
    """The persona cache is module-global; isolate every test from cross-test bleed."""
    identity_mod._persona_cache.clear()
    yield
    identity_mod._persona_cache.clear()


def _identity_row(**over):
    base = dict(
        agent_name="mimir",
        name="Mimir",
        role="Warden of the Library",
        persona="Guard the corpus; certify only trustworthy sources.",
        model=None,
        status="active",
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# system_prompt — composition
# ---------------------------------------------------------------------------


def test_system_prompt_composes_name_role_persona():
    i = Identity(**_identity_row())
    sp = system_prompt(i)
    assert sp == (
        "You are Mimir, the Warden of the Library of an autonomous AI research lab. "
        "Guard the corpus; certify only trustworthy sources."
    )


def test_system_prompt_with_empty_persona_strips_trailing_space():
    i = Identity(**_identity_row(persona=""))
    sp = system_prompt(i)
    assert sp == "You are Mimir, the Warden of the Library of an autonomous AI research lab."
    assert not sp.endswith(" ")


# ---------------------------------------------------------------------------
# load_identity / roster
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_identity_maps_row():
    pool = ScriptedPool(rules=[("FROM agent_identities WHERE agent_name", _identity_row())])
    i = await load_identity(pool, "mimir")
    assert isinstance(i, Identity)
    assert i.name == "Mimir"
    assert i.role == "Warden of the Library"
    assert i.model is None
    assert i.status == "active"


@pytest.mark.asyncio
async def test_load_identity_missing_row_returns_none():
    pool = ScriptedPool()  # no rules → fetchrow returns None
    assert await load_identity(pool, "nobody") is None


@pytest.mark.asyncio
async def test_load_identity_coerces_null_role_and_persona_to_empty():
    pool = ScriptedPool(rules=[("FROM agent_identities", _identity_row(role=None, persona=None))])
    i = await load_identity(pool, "mimir")
    assert i.role == ""
    assert i.persona == ""


@pytest.mark.asyncio
async def test_roster_returns_all_ordered():
    rows = [_identity_row(agent_name="ariadne", name="Ariadne"), _identity_row()]
    pool = ScriptedPool(rules=[("FROM agent_identities ORDER BY agent_name", rows)])
    out = await roster(pool)
    assert [i.name for i in out] == ["Ariadne", "Mimir"]


# ---------------------------------------------------------------------------
# persona_for — compose, cache, fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_for_composes_from_row():
    pool = ScriptedPool(rules=[("FROM agent_identities WHERE agent_name", _identity_row())])
    p = await persona_for(pool, "mimir")
    assert p is not None
    assert "You are Mimir, the Warden of the Library" in p


@pytest.mark.asyncio
async def test_persona_for_caches_and_skips_second_db_hit():
    calls = {"n": 0}
    pool = AsyncMock()

    async def _fetchrow(*_a, **_kw):
        calls["n"] += 1
        return _identity_row()

    pool.fetchrow.side_effect = _fetchrow
    first = await persona_for(pool, "mimir")
    second = await persona_for(pool, "mimir")
    assert first == second
    assert calls["n"] == 1  # second call served from the TTL cache


@pytest.mark.asyncio
async def test_persona_for_no_row_returns_none():
    pool = ScriptedPool()
    assert await persona_for(pool, "ghost") is None


@pytest.mark.asyncio
async def test_persona_for_empty_persona_returns_none():
    pool = ScriptedPool(rules=[("FROM agent_identities", _identity_row(persona=""))])
    assert await persona_for(pool, "mimir") is None


@pytest.mark.asyncio
async def test_persona_for_db_error_falls_back_to_none_without_raising():
    pool = AsyncMock()
    pool.fetchrow.side_effect = RuntimeError("db down")
    # Must not raise; a blip yields the code fallback (None) instead of breaking the prompt.
    assert await persona_for(pool, "mimir") is None


@pytest.mark.asyncio
async def test_persona_for_db_error_after_cached_returns_stale_value():
    pool = AsyncMock()
    state = {"fail": False}

    async def _fetchrow(*_a, **_kw):
        if state["fail"]:
            raise RuntimeError("db blip")
        return _identity_row()

    pool.fetchrow.side_effect = _fetchrow
    good = await persona_for(pool, "mimir")
    # Force a re-fetch past the TTL window, then make the DB fail.
    identity_mod._persona_cache["mimir"] = (good, identity_mod._persona_cache["mimir"][1] - 10_000)
    state["fail"] = True
    assert await persona_for(pool, "mimir") == good  # serves the last good value, doesn't raise


# ---------------------------------------------------------------------------
# Curator wiring — the registry persona reaches the system layer (positive path)
# ---------------------------------------------------------------------------


def _company_state():
    return SimpleNamespace(
        current_phase="exploration",
        phase_started_at=datetime.now(UTC) - timedelta(days=3),
        bootstrap_at=datetime.now(UTC) - timedelta(days=10),
        deadline=datetime.now(UTC) + timedelta(days=30),
        problem_statement="Find the niche.",
        stance="No hype.",
        success_criterion="Establish one rigorous result.",
        thesis=None,
        niche=None,
        audience=None,
        charter="SECRET — must not leak",
        paused=False,
        paused_reason=None,
    )


@pytest.mark.asyncio
async def test_curator_build_uses_registry_persona_over_code_fallback():
    # A registry persona that is deliberately NOT the SYSTEM_PROMPTS constant, so a match proves
    # the curator read the row (not the code anchor).
    row = _identity_row(role="Keeper of Scrolls", persona="Trust nothing unverified.")
    state = AsyncMock()
    state.pool = ScriptedPool(rules=[("FROM agent_identities WHERE agent_name", row)])
    state.get_company_state.return_value = _company_state()
    state.count_active_theses.return_value = 0
    memory = AsyncMock()
    lessons = AsyncMock()
    lessons.fetch_applicable.return_value = []

    cur = Curator(state, memory, lessons)
    prompt = await cur.build("mimir.certify", {"title": "T", "source_url": "http://u", "host": "h"})
    sys_msg = prompt.as_system_message()
    assert "Keeper of Scrolls" in sys_msg
    assert "Trust nothing unverified." in sys_msg


@pytest.mark.asyncio
async def test_curator_build_falls_back_when_no_identity_row():
    state = AsyncMock()
    state.pool = ScriptedPool()  # no agent_identities row
    state.get_company_state.return_value = _company_state()
    state.count_active_theses.return_value = 0
    memory = AsyncMock()
    lessons = AsyncMock()
    lessons.fetch_applicable.return_value = []

    cur = Curator(state, memory, lessons)
    prompt = await cur.build("mimir.certify", {"title": "T", "source_url": "http://u", "host": "h"})
    # The code anchor (SYSTEM_PROMPTS['mimir']) names Mimir's role.
    assert "Warden of the Library" in prompt.as_system_message()
