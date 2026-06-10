"""Tests for Mimir's acquire/pull-path adjudication — allow-list, cap, dedupe,
resolve. The fulfilled-ingest branch reuses ingest_source (covered in
test_ingest.py); here we exercise the deterministic guards that short-circuit
before any fetch."""

import pytest

import agents.mimir.acquire as acquire_mod
from agents.mimir.acquire import AcquireRequest, _resolve_candidates, handle_acquire_requested, request_acquire

pytestmark = pytest.mark.asyncio

_WHY = "this source grounds the speculative-decoding claim we are testing now"


async def test_resolve_candidates_query_uses_relevance_sort(monkeypatch):
    """A targeted acquire query must search arXiv by RELEVANCE, not newest-first — else a niche
    query falls back to the newest arXiv-wide submissions (off-topic papers). No DB needed."""
    seen: list[dict] = []

    async def _fake_scout(topics, per_topic, *, start=0, sort="submittedDate"):
        seen.append({"topics": topics, "sort": sort})
        return []  # empty → resolver stops after page 0

    monkeypatch.setattr(acquire_mod, "scout_arxiv", _fake_scout)
    await _resolve_candidates(AcquireRequest(requester="researcher", why=_WHY, query="deep kernel learning GP"))
    assert seen and all(c["sort"] == "relevance" for c in seen)
    assert seen[0]["topics"] == ["deep kernel learning GP"]


class _Disp:
    def __init__(self, state):
        self.state = state


async def test_request_acquire_allow_list(db):
    # researcher is on the allow-list and emits the request event.
    await request_acquire(db, AcquireRequest(requester="researcher", why=_WHY, arxiv_id="2401.11111"))
    n = await db.pool.fetchval("SELECT count(*) FROM events WHERE event_type = 'acquire.requested'")
    assert n == 1
    # a non-allowed role is refused at the lever.
    with pytest.raises(ValueError):
        await request_acquire(db, AcquireRequest(requester="intern", why=_WHY, arxiv_id="2401.22222"))


async def test_acquire_already_have(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    async with db.pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE source_kind = 'arxiv' AND canonical_key = '2401.55555'")
    try:
        await db.upsert_document(kind="paper", source_kind="arxiv", canonical_key="2401.55555", title="Have it")
        req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.55555")
        res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(db))
        assert res["status"] == "already_have"
        assert res["document_id"] is None or isinstance(res["document_id"], int)
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE source_kind = 'arxiv' AND canonical_key = '2401.55555'")


async def test_acquire_rate_limited(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    monkeypatch.setenv("MIMIR_ACQUIRE_CAP_PER_AGENT", "0")
    req = AcquireRequest(requester="novelty", why=_WHY, arxiv_id="2401.33333")
    await request_acquire(db, req)  # 1 acquire.requested -> count 1 > cap 0
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(db))
    assert res["status"] == "rate_limited"


async def test_acquire_unresolvable_rejected(db, monkeypatch):
    monkeypatch.setenv("MIMIR_LOOP", "on")
    req = AcquireRequest(requester="pi", why=_WHY)  # no identifier, no query
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(db))
    assert res["status"] == "rejected"
    assert "resolve" in res["reason"]


async def test_acquire_gated_off_is_noop(db, monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.44444")
    assert await handle_acquire_requested({"payload": req.model_dump()}, _Disp(db)) is None
