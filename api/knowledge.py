"""
Knowledge / Library stats — corpus + graph counts for the Flow page's Knowledge
column (Ingestion -> RAG Corpus -> Knowledge Graph).

    GET /knowledge/stats

Corpus counts come from Postgres (migration 015: documents/chunks/datasets); the
graph counts come from Neo4j (Paper/Dataset/CITES), wrapped in the same
try/except -> "unavailable" pattern as /trace/graph/stats. Until migration 015 is
applied the corpus block returns zeros with status "planned" so the page renders
the Knowledge nodes in a planned state rather than 500-ing.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Request

from library.corpus.tools import corpus_search

log = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _planned_corpus() -> dict:
    """Zeroed corpus block — rendered before migration 015 lands."""
    return {
        "status": "planned",
        "documents_by_kind": {},
        "docs_by_trust_tier": {},
        "by_status": {},
        "chunks": 0,
        "chunks_embedded": 0,
        "datasets": 0,
        "docs_today": 0,
    }


async def _corpus_stats(pool) -> dict:
    """Counts from the pgvector corpus tables. Raises UndefinedTableError pre-015."""
    async with pool.acquire() as conn:
        by_kind = await conn.fetch("SELECT kind::text AS k, COUNT(*) AS c FROM documents GROUP BY kind")
        by_tier = await conn.fetch("SELECT trust_tier::text AS t, COUNT(*) AS c FROM documents GROUP BY trust_tier")
        by_status = await conn.fetch("SELECT status::text AS s, COUNT(*) AS c FROM documents GROUP BY status")
        chunks = await conn.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedded FROM chunks"
        )
        datasets = await conn.fetchval("SELECT COUNT(*) FROM datasets")
        docs_today = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE COALESCE(certified_at, ingested_at) >= date_trunc('day', now())"
        )
    return {
        "status": "ok",
        "documents_by_kind": {r["k"]: r["c"] for r in by_kind},
        "docs_by_trust_tier": {r["t"]: r["c"] for r in by_tier},
        "by_status": {r["s"]: r["c"] for r in by_status},
        "chunks": chunks["total"],
        "chunks_embedded": chunks["embedded"],
        "datasets": datasets,
        "docs_today": docs_today,
    }


async def _memory_counts(pool) -> dict:
    """Structured-memory counts from Postgres (claims + experiment runs)."""
    out = {"claims": 0, "experiments": 0}
    try:
        async with pool.acquire() as conn:
            out["claims"] = await conn.fetchval("SELECT COUNT(*) FROM claims") or 0
            try:
                out["experiments"] = await conn.fetchval("SELECT COUNT(*) FROM experiment_runs") or 0
            except Exception:  # noqa: BLE001 — table may not exist
                out["experiments"] = 0
    except Exception as e:  # noqa: BLE001
        log.warning("memory counts failed: %s", e)
    return out


async def _graph_stats() -> dict:
    """Neo4j Paper/Dataset/CITES counts. 'unavailable' if Neo4j is down; the labels
    simply return 0 until the KG extension (Phase 1/2) populates them."""
    try:
        from library.graph.tools import _get_driver

        driver = await _get_driver()
        async with driver.session() as session:
            nodes = await session.run("MATCH (n) RETURN COUNT(n) AS count")
            papers = await session.run("MATCH (p:Paper) RETURN COUNT(p) AS count")
            datasets = await session.run("MATCH (d:Dataset) RETURN COUNT(d) AS count")
            citations = await session.run("MATCH (:Finding)-[:CITES]->(:Paper) RETURN COUNT(*) AS count")
            return {
                "status": "ok",
                "nodes": (await nodes.data())[0]["count"],
                "papers": (await papers.data())[0]["count"],
                "datasets": (await datasets.data())[0]["count"],
                "citations": (await citations.data())[0]["count"],
            }
    except Exception as e:  # noqa: BLE001 — mirror /trace/graph/stats
        log.warning("knowledge graph_stats failed: %s", e)
        return {"status": "unavailable", "error": str(e)}


@router.get("/stats")
async def knowledge_stats(request: Request) -> dict:
    """Corpus + graph counts for the Flow Knowledge column. Degrades gracefully:
    the corpus block is 'planned' (zeros) until migration 015 lands; the graph
    block is 'unavailable' if Neo4j is down."""
    pool = request.app.state.pool
    try:
        corpus = await _corpus_stats(pool)
    except asyncpg.UndefinedTableError:
        corpus = _planned_corpus()
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard on stats
        log.warning("knowledge corpus stats failed: %s", e)
        corpus = {**_planned_corpus(), "status": "error", "error": str(e)}
    graph = await _graph_stats()
    memory = await _memory_counts(pool)
    return {"corpus": corpus, "graph": graph, "memory": memory}


@router.get("/recent")
async def knowledge_recent(request: Request, limit: int = 8) -> dict:
    """Latest ingested documents for the Library's 'recent ingests' feed."""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, source_kind, arxiv_id, source_url, status, "
                "COALESCE(certified_at, ingested_at) AS at "
                "FROM documents ORDER BY COALESCE(certified_at, ingested_at) DESC NULLS LAST LIMIT $1",
                min(max(limit, 1), 20),
            )
            today = await conn.fetchval(
                "SELECT COUNT(*) FROM documents WHERE COALESCE(certified_at, ingested_at) >= date_trunc('day', now())"
            )
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("knowledge recent failed: %s", e)
        return {"status": "error", "error": str(e), "today": 0, "items": []}
    return {
        "status": "ok",
        "today": today or 0,
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "source_kind": r["source_kind"],
                "arxiv_id": r["arxiv_id"],
                "source_url": r["source_url"],
                "status": r["status"],
                "at": r["at"].isoformat() if r["at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/search")
async def knowledge_search(q: str = "", k: int = 6) -> dict:
    """Semantic search over the certified corpus (the Library inspector's search
    box). Embeds the query + ANN over chunks; degrades to empty on any failure so
    the dashboard never 500s."""
    q = (q or "").strip()
    if not q:
        return {"status": "ok", "query": "", "hits": []}
    try:
        hits = await corpus_search(q, k=min(max(k, 1), 12))
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard on a search
        log.warning("corpus search failed: %s", e)
        return {"status": "error", "error": str(e), "query": q, "hits": []}
    return {
        "status": "ok",
        "query": q,
        "hits": [
            {
                "document_id": h.document_id,
                "title": h.title,
                "source_url": h.source_url,
                "trust_tier": h.trust_tier,
                "score": round(h.score, 4),
                "snippet": (h.text or "")[:240],
            }
            for h in hits
        ],
    }
