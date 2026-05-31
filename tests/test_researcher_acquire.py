"""The Researcher -> Mimir pull hook: a gap_check gap becomes an
`acquire.requested` for the missing source. Exercises `_request_gap_sources` in
isolation (no LLM, no full loop) against the `db` fixture (events truncated)."""

import json
from types import SimpleNamespace

import pytest

from agents.researcher.loop import _request_gap_sources
from agents.researcher.schemas import GapCheck

pytestmark = pytest.mark.asyncio


def _gap(gaps: list[str]) -> GapCheck:
    return GapCheck(has_gaps=bool(gaps), gaps=gaps, proposed_followups=[], should_iterate=False, reason="test")


async def test_gap_fires_acquire_request_when_mimir_on(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    await _request_gap_sources(_gap(["KV cache compression methods"]), SimpleNamespace(claim_id=42), db)

    row = await db.pool.fetchrow("SELECT payload FROM events WHERE event_type = 'acquire.requested'")
    assert row is not None
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert payload["requester"] == "researcher"
    assert payload["query"] == "KV cache compression methods"
    assert payload["claim_id"] == 42
    assert len(payload["why"]) >= 30


async def test_no_request_when_mimir_off(db, monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    await _request_gap_sources(_gap(["something missing"]), SimpleNamespace(claim_id=1), db)
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type = 'acquire.requested'")
    assert n == 0


async def test_no_request_when_no_gaps(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    await _request_gap_sources(_gap([]), SimpleNamespace(claim_id=1), db)
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type = 'acquire.requested'")
    assert n == 0
