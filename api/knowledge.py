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

import json
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


def _acquire_row(r) -> dict:
    """Collapse an acquire.* event into a request-feed row (requester + ask +
    resolution status), defensively reading the payload."""
    payload = r["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload) if payload else {}
    payload = payload or {}
    status = {
        "acquire.requested": "requested",
        "acquire.fulfilled": "fulfilled",
        "acquire.rejected": "rejected",
    }.get(r["event_type"], r["event_type"])
    ask = payload.get("query") or payload.get("topic") or payload.get("reason") or payload.get("gap")
    requester = payload.get("requested_by") or payload.get("requester") or payload.get("agent") or "—"
    return {
        "requester": str(requester),
        "ask": (str(ask)[:120] if ask else None),
        "status": status,
        "at": r["emitted_at"].isoformat() if r["emitted_at"] else None,
    }


@router.get("/mimir")
async def mimir_panel(request: Request) -> dict:
    """Rich panel for the Warden (Mimir): at-a-glance counts with today's deltas,
    the trust ladder, today's intake funnel (from events), the corpus source mix,
    recent certifications, and the acquire-request feed. All real data; degrades
    to status='planned'/'error' rather than 500-ing the dashboard."""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            by_status = {
                r["s"]: r["c"]
                for r in await conn.fetch("SELECT status::text s, COUNT(*) c FROM documents GROUP BY status")
            }
            by_tier = {
                r["t"]: r["c"]
                for r in await conn.fetch("SELECT trust_tier::text t, COUNT(*) c FROM documents GROUP BY trust_tier")
            }
            mix = await conn.fetch("SELECT source_kind, COUNT(*) c FROM documents GROUP BY source_kind ORDER BY c DESC")
            ev_today = {
                r["e"]: r["c"]
                for r in await conn.fetch(
                    "SELECT event_type e, COUNT(*) c FROM events "
                    "WHERE emitted_at >= date_trunc('day', now()) GROUP BY event_type"
                )
            }
            ingested_yday = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'document.ingested' "
                    "AND emitted_at >= date_trunc('day', now()) - interval '1 day' "
                    "AND emitted_at < date_trunc('day', now())"
                )
                or 0
            )
            pending = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM documents WHERE status NOT IN ('certified', 'quarantined', 'blocked')"
                )
                or 0
            )
            recent_cert = await conn.fetch(
                "SELECT title, source_kind, arxiv_id, canonical_key, COALESCE(certified_at, ingested_at) at "
                "FROM documents WHERE status = 'certified' "
                "ORDER BY COALESCE(certified_at, ingested_at) DESC NULLS LAST LIMIT 6"
            )
            requests = await conn.fetch(
                "SELECT event_type, payload, emitted_at FROM events "
                "WHERE event_type IN ('acquire.requested', 'acquire.fulfilled', 'acquire.rejected') "
                "ORDER BY emitted_at DESC LIMIT 6"
            )
    except asyncpg.UndefinedTableError:
        return {"status": "planned"}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("mimir panel stats failed: %s", e)
        return {"status": "error", "error": str(e)}

    total_mix = sum(r["c"] for r in mix) or 1
    return {
        "status": "ok",
        "at_a_glance": {
            "certified": by_status.get("certified", 0),
            "certified_today": ev_today.get("document.ingested", 0),
            "quarantined": by_status.get("quarantined", 0) + by_status.get("blocked", 0),
            "quarantined_today": ev_today.get("mimir.ingest_blocked", 0),
            "pending": pending,
            "ingested_today": ev_today.get("document.ingested", 0),
            "ingested_yesterday": ingested_yday,
        },
        "trust_ladder": by_tier,
        "pipeline_today": {
            "discovered": ev_today.get("source.discovered", 0),
            "parsed": ev_today.get("document.parsed", 0),
            "ingested": ev_today.get("document.ingested", 0),
            "quarantined": ev_today.get("mimir.ingest_blocked", 0),
        },
        "source_mix": [{"kind": r["source_kind"], "count": r["c"], "pct": round(100 * r["c"] / total_mix)} for r in mix],
        "recent_certifications": [
            {
                "title": r["title"],
                "source_kind": r["source_kind"],
                "arxiv_id": r["arxiv_id"],
                "canonical_key": r["canonical_key"],
                "at": r["at"].isoformat() if r["at"] else None,
            }
            for r in recent_cert
        ],
        "requests": [_acquire_row(r) for r in requests],
    }


_SCOUT_KINDS = {"arxiv", "web", "github", "dataset"}


@router.get("/scout")
async def scout_panel(request: Request, kind: str) -> dict:
    """Interpretable per-scout view, pulled durably from the corpus (NOT the live
    event window, which ages out): how many of this source kind are in the
    Library, how many today, the topics last searched (from the most recent
    library.trends), and the most recent items it surfaced — paper titles for
    arXiv, title+url+summary for web, owner/repo for github, dataset ids for the
    dataset scout — each with a first-chunk snippet for context."""
    kind = (kind or "").strip().lower()
    if kind not in _SCOUT_KINDS:
        return {"status": "error", "error": f"unknown scout kind {kind!r}"}
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            in_corpus = await conn.fetchval("SELECT COUNT(*) FROM documents WHERE source_kind = $1", kind) or 0
            added_today = (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM documents WHERE source_kind = $1 "
                    "AND COALESCE(certified_at, ingested_at) >= date_trunc('day', now())",
                    kind,
                )
                or 0
            )
            recent = await conn.fetch(
                """
                SELECT d.title, d.source_url, d.arxiv_id, d.canonical_key, d.status,
                       COALESCE(d.certified_at, d.ingested_at) AS at,
                       (SELECT c.text FROM chunks c WHERE c.document_id = d.id ORDER BY c.ordinal LIMIT 1) AS snippet
                FROM documents d
                WHERE d.source_kind = $1
                ORDER BY COALESCE(d.certified_at, d.ingested_at) DESC NULLS LAST
                LIMIT 8
                """,
                kind,
            )
            trends = await conn.fetchrow(
                "SELECT payload, emitted_at FROM events WHERE event_type = 'library.trends' "
                "ORDER BY emitted_at DESC LIMIT 1"
            )
    except asyncpg.UndefinedTableError:
        return {"status": "planned", "source_kind": kind}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("scout panel (%s) failed: %s", kind, e)
        return {"status": "error", "error": str(e), "source_kind": kind}

    topics: list[str] = []
    searched_at = None
    if trends:
        payload = trends["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        topics = (payload or {}).get("topics") or []
        searched_at = trends["emitted_at"].isoformat() if trends["emitted_at"] else None

    return {
        "status": "ok",
        "source_kind": kind,
        "in_corpus": in_corpus,
        "added_today": added_today,
        "last_searched": {"topics": topics[:12], "at": searched_at},
        "recent": [
            {
                "title": r["title"],
                "source_url": r["source_url"],
                "arxiv_id": r["arxiv_id"],
                "canonical_key": r["canonical_key"],
                "status": r["status"],
                "snippet": ((r["snippet"] or "").strip()[:240] or None),
                "at": r["at"].isoformat() if r["at"] else None,
            }
            for r in recent
        ],
    }


def _payload(p) -> dict:
    if isinstance(p, str):
        return json.loads(p) if p else {}
    return p or {}


@router.get("/gate")
async def gate_panel(request: Request) -> dict:
    """The intake gate (the lab's 'entrance'): who was admitted and who was turned
    away, with reasons. Two gates feed it — TRUST blocks (mimir.ingest_blocked:
    license / retraction / untrusted) and QUALITY rejections
    (library.ingest_rejected: too thin / non-content page). Plus today's tallies
    and a recent-admitted list so decisions can be audited both ways."""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            ev_today = {
                r["e"]: r["c"]
                for r in await conn.fetch(
                    "SELECT event_type e, COUNT(*) c FROM events "
                    "WHERE emitted_at >= date_trunc('day', now()) GROUP BY event_type"
                )
            }
            quarantined = await conn.fetchval("SELECT COUNT(*) FROM documents WHERE status = 'blocked'") or 0
            blocked = await conn.fetch(
                "SELECT e.emitted_at, e.payload, d.title, d.source_kind, d.source_url "
                "FROM events e LEFT JOIN documents d ON d.id = e.target_id "
                "WHERE e.event_type = 'mimir.ingest_blocked' ORDER BY e.emitted_at DESC LIMIT 15"
            )
            rejected = await conn.fetch(
                "SELECT emitted_at, payload FROM events WHERE event_type = 'library.ingest_rejected' "
                "ORDER BY emitted_at DESC LIMIT 15"
            )
            admitted = await conn.fetch(
                "SELECT title, source_kind, arxiv_id, canonical_key, trust_tier, "
                "COALESCE(certified_at, ingested_at) at FROM documents WHERE status = 'certified' "
                "ORDER BY COALESCE(certified_at, ingested_at) DESC NULLS LAST LIMIT 8"
            )
    except asyncpg.UndefinedTableError:
        return {"status": "planned"}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("gate panel failed: %s", e)
        return {"status": "error", "error": str(e)}

    turned_away = []
    for r in blocked:
        p = _payload(r["payload"])
        turned_away.append(
            {
                "gate": "trust",
                "title": r["title"],
                "url": r["source_url"],
                "source_kind": r["source_kind"],
                "reason": p.get("reasons") or "blocked by trust gate",
                "at": r["emitted_at"].isoformat() if r["emitted_at"] else None,
            }
        )
    for r in rejected:
        p = _payload(r["payload"])
        turned_away.append(
            {
                "gate": "quality",
                "title": p.get("title"),
                "url": p.get("url"),
                "source_kind": p.get("source_kind"),
                "reason": p.get("reason") or "failed quality gate",
                "at": r["emitted_at"].isoformat() if r["emitted_at"] else None,
            }
        )
    turned_away.sort(key=lambda x: x["at"] or "", reverse=True)

    return {
        "status": "ok",
        "today": {
            "admitted": ev_today.get("document.ingested", 0),
            "blocked_trust": ev_today.get("mimir.ingest_blocked", 0),
            "rejected_quality": ev_today.get("library.ingest_rejected", 0),
            "discovered": ev_today.get("source.discovered", 0),
        },
        "quarantined": quarantined,
        "turned_away": turned_away[:16],
        "admitted": [
            {
                "title": r["title"],
                "source_kind": r["source_kind"],
                "arxiv_id": r["arxiv_id"],
                "canonical_key": r["canonical_key"],
                "trust_tier": r["trust_tier"],
                "at": r["at"].isoformat() if r["at"] else None,
            }
            for r in admitted
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
