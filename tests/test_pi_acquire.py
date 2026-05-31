"""The PI -> Mimir acquire hook: mission gaps flagged in a phase-transition
decision (PhaseTransitionDecision.needed_sources) become acquire.requested.
Exercises _request_pi_sources in isolation against the `db` fixture."""

import json

import pytest

from agents.pi.phase_transition import _request_pi_sources

pytestmark = pytest.mark.asyncio


async def test_pi_gap_fires_acquire_when_mimir_on(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    await _request_pi_sources(["scaling laws for mixture-of-experts"], db)

    rows = await db.pool.fetch("SELECT payload FROM events WHERE event_type = 'acquire.requested'")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    assert payload["requester"] == "pi"
    assert "scaling laws" in payload["query"]


async def test_pi_no_request_when_mimir_off(db, monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    await _request_pi_sources(["something missing"], db)
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type = 'acquire.requested'")
    assert n == 0
