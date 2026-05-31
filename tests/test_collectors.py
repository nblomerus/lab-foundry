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

    monkeypatch.setattr(collectors, "scout_arxiv", _fake_scout)
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

    monkeypatch.setattr(collectors, "scout_arxiv", _fake_scout)
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


def test_discovery_topics_default(monkeypatch):
    from agents.mimir.collectors import discovery_topics

    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    assert len(discovery_topics()) >= 1

    monkeypatch.setenv("LIBRARY_TOPICS", " a , b ,, c ")
    assert discovery_topics() == ["a", "b", "c"]
