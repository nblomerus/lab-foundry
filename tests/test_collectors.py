"""Tests for Mimir's data collectors — the discovery sweep that emits
`source.discovered`. Uses the `db` fixture (events are truncated per test);
scout_arxiv is mocked so there's no network.
"""

import json

import pytest

from library.ingest.scouts import SourceDescriptor

pytestmark = pytest.mark.asyncio


def _descriptors() -> list[SourceDescriptor]:
    return [
        SourceDescriptor(
            kind="paper",
            source_kind="test_arxiv",
            canonical_key="2401.00001",
            url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            title="Paper A",
            why="test topic",
        ),
        SourceDescriptor(
            kind="paper",
            source_kind="test_arxiv",
            canonical_key="2401.00002",
            url="https://arxiv.org/abs/2401.00002",
            arxiv_id="2401.00002",
            title="Paper B",
            why="test topic",
        ),
    ]


async def _clean(db):
    async with db.pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE source_kind = 'test_arxiv'")


async def test_sweep_emits_source_discovered_for_new(db, monkeypatch):
    import agents.mimir.collectors as collectors

    async def _fake_scout(topics, per_topic=5):
        return _descriptors()

    monkeypatch.setitem(collectors._SCOUTS, "arxiv", _fake_scout)
    await _clean(db)
    try:
        res = await collectors.run_discovery_sweep(["anything"], db)
        assert res["scanned"] == 2
        assert res["discovered"] == 2

        rows = await db.pool.fetch("SELECT payload FROM events WHERE event_type = 'source.discovered' ORDER BY id")
        assert len(rows) == 2
        payload = rows[0]["payload"]
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload["source"]["source_kind"] == "test_arxiv"
        assert payload["source"]["canonical_key"] == "2401.00001"
    finally:
        await _clean(db)


async def test_sweep_skips_already_ingested(db, monkeypatch):
    import agents.mimir.collectors as collectors

    async def _fake_scout(topics, per_topic=5):
        return _descriptors()

    monkeypatch.setitem(collectors._SCOUTS, "arxiv", _fake_scout)
    await _clean(db)
    try:
        # Pre-ingest the first descriptor; the sweep should skip it.
        await db.upsert_document(kind="paper", source_kind="test_arxiv", canonical_key="2401.00001", title="Paper A")

        res = await collectors.run_discovery_sweep(["anything"], db)
        assert res["scanned"] == 2
        assert res["discovered"] == 1

        n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type = 'source.discovered'")
        assert n == 1
    finally:
        await _clean(db)


async def test_sweep_emits_trends_digest(db, monkeypatch):
    import agents.mimir.collectors as collectors

    async def _fake_scout(topics, per_topic=5):
        return _descriptors()

    monkeypatch.setitem(collectors._SCOUTS, "arxiv", _fake_scout)
    await _clean(db)
    try:
        await collectors.run_discovery_sweep(["anything"], db)
        row = await db.pool.fetchrow("SELECT payload FROM events WHERE event_type = 'library.trends'")
        assert row is not None
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        assert payload["count"] == 2
        assert len(payload["new"]) == 2
    finally:
        await _clean(db)


async def test_default_sweep_topics_track_active_claims(db, monkeypatch):
    from agents.mimir.collectors import default_sweep_topics

    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)  # Ariadne active -> tracks agenda
    await db.create_claim("speculative decoding for faster LLM inference", 0.6)
    topics = await default_sweep_topics(db)
    assert any("speculative decoding" in t for t in topics)  # agenda steers discovery
    assert len(topics) > 1  # frontier defaults still present


async def test_plan_sweep_aggressive_when_ariadne_dark(monkeypatch):
    """KNOWLEDGE_CORE_ONLY (Ariadne dark) -> broad, deep, agenda-free sweep."""
    from agents.mimir.collectors import _AGGRESSIVE_PER_TOPIC, _AGGRESSIVE_TOPICS, plan_sweep

    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "1")

    class _NoState:  # the aggressive branch never queries the DB
        pass

    topics, per_topic = await plan_sweep(_NoState())
    assert len(topics) == _AGGRESSIVE_TOPICS
    assert per_topic == _AGGRESSIVE_PER_TOPIC
    assert len(set(topics)) == len(topics)  # no dupes in the rotating slice


def test_discovery_topics_default(monkeypatch):
    from agents.mimir.collectors import discovery_topics

    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    assert len(discovery_topics()) >= 1

    monkeypatch.setenv("LIBRARY_TOPICS", " a , b ,, c ")
    assert discovery_topics() == ["a", "b", "c"]
