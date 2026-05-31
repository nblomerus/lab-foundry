"""
Integration test for labfoundry_corpus retrieval over pgvector.

SKIPS unless a migrated DB is reachable via DATABASE_URL — the rest of the suite
fakes the DB, but this one needs the real migration-015 schema + the pgvector
extension. It seeds rows with KNOWN vectors and exercises `_search_by_vector`
directly, so it NEVER needs Ollama. It cleans up its own rows
(source_kind='test_corpus_pytest').

Proves the two load-bearing read-path invariants:
  1. the certified+queryable gate excludes blocked/non-queryable docs, and
  2. the `min_trust` floor filters out docs below the requested tier,
and that nearest + highest-trust ranks first under the §6 rerank.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("pgvector")  # skip cleanly if the dep isn't installed
import asyncpg  # noqa: E402

TEST_SOURCE_KIND = "test_corpus_pytest"
EMBED_DIM = 768


def _unit_vec(idx: int) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[idx] = 1.0
    return v


async def _dsn_or_skip() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set — corpus integration test needs a migrated DB")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"DB unreachable: {e}")
    try:
        has_chunks = await conn.fetchval("SELECT to_regclass('public.chunks') IS NOT NULL")
        if not has_chunks:
            pytest.skip("migration 015 not applied (no chunks table)")
    finally:
        await conn.close()
    return dsn


async def _seed(pool, title, tier, status, state, queryable, vec) -> int:
    async with pool.acquire() as conn:
        doc_id = await conn.fetchval(
            """
            INSERT INTO documents (kind, title, source_kind, canonical_key,
                   status, trust_tier, trust_state, queryable)
            VALUES ('paper', $1, $2, $3,
                    $4::document_status, $5::trust_tier, $6::trust_state, $7)
            RETURNING id
            """,
            title,
            TEST_SOURCE_KIND,
            f"{TEST_SOURCE_KIND}:{title}",
            status,
            tier,
            state,
            queryable,
        )
        await conn.execute(
            """
            INSERT INTO chunks (document_id, ordinal, text, embedding, content_hash)
            VALUES ($1, 0, $2, $3, $4)
            """,
            doc_id,
            f"chunk for {title}",
            vec,
            f"{TEST_SOURCE_KIND}:{title}:0",
        )
    return doc_id


@pytest.mark.asyncio
async def test_corpus_search_respects_trust_floor_and_gate():
    await _dsn_or_skip()
    from labfoundry.mcp_servers.labfoundry_corpus import tools

    pool = await tools._get_pool()
    async with pool.acquire() as conn:  # clean any leftovers from a prior run
        await conn.execute("DELETE FROM documents WHERE source_kind = $1", TEST_SOURCE_KIND)

    try:
        # A: near the query (e0), top tier, certified+queryable -> should rank #1
        a = await _seed(pool, "A peer", "peer_reviewed", "certified", "certified", True, _unit_vec(0))
        # B: orthogonal (e1), low tier, certified+queryable -> returned but lower
        b = await _seed(pool, "B web", "web_unknown", "certified", "provisional", True, _unit_vec(1))
        # C: near the query (e0) but BLOCKED + not queryable -> must be excluded
        c = await _seed(pool, "C blocked", "web_reputable", "blocked", "quarantined", False, _unit_vec(0))

        query = _unit_vec(0)
        res = await tools._search_by_vector(query, k=5)
        ids = [r.document_id for r in res]

        assert a in ids, "certified+queryable doc near the query must be returned"
        assert c not in ids, "blocked/non-queryable doc must be excluded by the gate"
        assert ids[0] == a, "nearest + highest-trust doc must rank first under the rerank"

        # min_trust floor: web_reputable (rank 3) excludes web_unknown (rank 2)
        res2 = await tools._search_by_vector(query, k=5, min_trust="web_reputable")
        ids2 = [r.document_id for r in res2]
        assert a in ids2, "peer_reviewed is above the web_reputable floor"
        assert b not in ids2, "web_unknown must be filtered out below the web_reputable floor"
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE source_kind = $1", TEST_SOURCE_KIND)
