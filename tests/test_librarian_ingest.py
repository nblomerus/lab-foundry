"""
Integration test for the Librarian ingest loop (phase A + phase B).

Exercises the full deterministic pipeline END-TO-END against a migration-015 DB
with the NETWORK and the EMBEDDER monkeypatched, so it needs NEITHER a live
fetch NOR Ollama:

  * `web_fetch` is patched to return a canned multi-section paper body.
  * the corpus embedder's `_get_embedder()` is patched to return a deterministic
    768-d unit vector — so phase B embeds without ever touching Ollama.

It asserts the load-bearing invariants of the ingest contract:
  1. phase A creates a `documents` row + stages chunks (NO vectors yet);
  2. phase B embeds those chunks (vectors present) and flips `queryable`;
  3. once queryable+certified, `_search_by_vector` (and thus `corpus_search`)
     would find the document.

SKIPS cleanly when DATABASE_URL is unset or the `chunks` table is absent
(mirrors tests/test_labfoundry_corpus.py). Uses a unique
source_kind='test_librarian_ingest' and DELETEs those rows in a finally block.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("pgvector")  # skip cleanly if the dep isn't installed
import asyncpg  # noqa: E402
import pgvector.asyncpg  # noqa: E402

TEST_SOURCE_KIND = "test_librarian_ingest"
TEST_CANONICAL_KEY = "test_librarian_ingest:paper-1"
EMBED_DIM = 768

# A multi-section paper body so the parser finds real sections and the chunker
# emits several chunks (markdown headers drive `extract_sections`).
_PAPER_BODY = """# Deterministic Retrieval over Trusted Corpora

## Abstract
We study retrieval-augmented generation (RAG) over a trust-graded corpus.
Maximum Inner Product Search (MIPS) is the workhorse of dense retrieval and we
analyze its behaviour under a certified-document gate.

## Introduction
Dense retrieval has become the dominant paradigm for open-domain question
answering. In this work we revisit the assumption that all documents in a
corpus are equally trustworthy. We introduce a trust ladder and show that
weighting by provenance improves answer precision without harming recall.
Our method is deterministic and adds no learned parameters to the retriever.

## Method
Given a query q we embed it into a 768-dimensional vector and run approximate
nearest-neighbour search over the chunk index. Each candidate chunk inherits
its document's trust tier. We rerank candidates by a convex combination of
cosine similarity, a trust weight, and a recency term. The trust weight is a
fixed lookup keyed by tier, so the reranker introduces no training signal and
remains fully reproducible across runs.

## Experiments
We evaluate on three open-domain QA benchmarks. Across all three, adding the
trust-weighted rerank improves exact-match by between 1.8 and 3.4 points over a
similarity-only baseline, while leaving recall@100 unchanged. Ablating the
recency term costs 0.4 points on the most time-sensitive benchmark and is
neutral elsewhere.

## Conclusion
A deterministic, provenance-aware rerank is a cheap and reproducible win for
RAG over heterogeneous corpora. We release the trust ladder and the rerank
weights so the results can be reproduced exactly.

## References
[1] Some Author. A prior paper. 2023.
[2] Another Author. Yet another paper. 2024.
"""


def _unit_vec(idx: int = 0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[idx % EMBED_DIM] = 1.0
    return v


class _FakeFetchedPage:
    """Mimics fetcher.FetchedPage's duck type (only .content / .url are read)."""

    def __init__(self, url: str, content: str):
        self.url = url
        self.content = content
        self.extractor = "test"
        self.status_code = 200
        self.bytes_fetched = len(content)
        self.from_cache = False


class _FakeEmbedder:
    """Stand-in for labfoundry_corpus.tools.Embedder — deterministic, no Ollama."""

    async def embed(self, text: str) -> list[float]:
        # All chunks share the same unit vector so a query at e0 retrieves them.
        return _unit_vec(0)


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register the pgvector codec on the test pool so set_chunk_embeddings can
    bind list[float] as vector(768) (mirrors labfoundry_corpus._init_conn)."""
    await pgvector.asyncpg.register_vector(conn)


async def _dsn_or_skip() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set — librarian ingest test needs a migrated DB")
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


@pytest.mark.asyncio
async def test_librarian_ingest_phase_a_then_b(monkeypatch):
    dsn = await _dsn_or_skip()

    from labfoundry.mcp_servers.labfoundry_corpus import tools as corpus_tools
    from labfoundry.research.librarian import loop as librarian_loop
    from labfoundry.state.client import PostgresClient

    fetched_url = "https://example.test/librarian/paper-1"

    # --- patch the network: web_fetch returns the canned paper body ---
    async def _fake_web_fetch(url, state, *, force=False, client=None):
        return _FakeFetchedPage(fetched_url, _PAPER_BODY)

    monkeypatch.setattr(librarian_loop, "web_fetch", _fake_web_fetch)

    # --- patch the embedder: deterministic 768-d vector, no Ollama ---
    _fake = _FakeEmbedder()

    async def _fake_get_embedder():
        return _fake

    monkeypatch.setattr(corpus_tools, "_get_embedder", _fake_get_embedder)

    # The loop writes embeddings through state.set_chunk_embeddings, so the pool
    # backing the state client MUST register the pgvector codec.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=_init_conn)
    state = PostgresClient(pool=pool)

    async with pool.acquire() as conn:  # clean any leftovers from a prior run
        await conn.execute("DELETE FROM documents WHERE source_kind = $1", TEST_SOURCE_KIND)

    try:
        source = {
            "kind": "paper",
            "source_kind": TEST_SOURCE_KIND,
            "canonical_key": TEST_CANONICAL_KEY,
            "url": fetched_url,
            "arxiv_id": None,
            "doi": None,
            "title": "Deterministic Retrieval over Trusted Corpora",
            "why": "test fixture",
        }

        # ---- PHASE A: fetch -> parse -> chunk-plan -> stage (no vectors) ----
        res_a = await librarian_loop.run_ingest_phase_a(source, state)
        assert res_a.get("awaiting") == "mimir", f"phase A should await Mimir: {res_a}"
        doc_id = res_a["document_id"]
        assert doc_id is not None
        n_chunks = res_a["n_chunks"]
        assert n_chunks > 0, "the multi-section paper must produce chunks"

        # documents row created.
        async with pool.acquire() as conn:
            doc_row = await conn.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)
        assert doc_row is not None
        assert doc_row["source_kind"] == TEST_SOURCE_KIND
        assert doc_row["queryable"] is False, "phase A must NOT flip queryable"
        assert doc_row["content_hash"], "content_hash should be set in phase A"

        # chunks staged WITHOUT embeddings.
        plan = await state.get_chunk_plan(doc_id)
        assert len(plan) == n_chunks
        assert all(not c["has_embedding"] for c in plan), "phase A must not embed"

        # Re-running phase A dedupes (same canonical_key).
        res_a2 = await librarian_loop.run_ingest_phase_a(source, state)
        assert res_a2.get("deduped") is True, f"re-run should dedupe: {res_a2}"

        # ---- PHASE B: embed -> KG (best-effort) -> flip queryable ----
        res_b = await librarian_loop.run_ingest_phase_b(doc_id, state)
        assert res_b.get("queryable") is True, f"phase B should flip queryable: {res_b}"
        assert res_b.get("embedded") == n_chunks, "all staged chunks should embed"

        # chunks now embedded.
        plan_after = await state.get_chunk_plan(doc_id)
        assert all(c["has_embedding"] for c in plan_after), "phase B must embed all chunks"

        # document queryable.
        doc_after = await state.get_document(doc_id)
        assert doc_after["queryable"] is True

        # ---- retrieval would find it, once certified+queryable ----
        # _search_by_vector requires status='certified' AND queryable. Mimir
        # (not yet built) would certify; simulate that here so we prove the
        # read path sees the freshly-ingested chunks.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE documents SET status = 'certified', trust_state = 'certified', "
                "trust_tier = 'preprint' WHERE id = $1",
                doc_id,
            )

        res = await corpus_tools._search_by_vector(_unit_vec(0), k=5)
        ids = [r.document_id for r in res]
        assert doc_id in ids, "the ingested+certified document must be retrievable"
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE source_kind = $1", TEST_SOURCE_KIND)
        await pool.close()
