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
import re

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
            trends_row = await conn.fetchrow(
                "SELECT payload FROM events WHERE event_type = 'library.trends' ORDER BY emitted_at DESC LIMIT 1"
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
        "focus_topics": (_payload(trends_row["payload"]).get("topics") if trends_row else []) or [],
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


_SCOUT_KINDS = {"arxiv", "web", "github", "dataset", "openml"}


# Each discovered source records the topic that surfaced it in `why`, formatted
# per scout (e.g. "arxiv topic: continual learning", "openml dataset (topic: X)",
# "dataset topic: Y (HF downloads=N)"). Pull out the clean topic; drop claim-like
# sentences and noise (anything without "topic:" or longer than a short phrase).
def _parse_topic(why: str | None) -> str | None:
    if not why:
        return None
    m = re.search(r"topic:\s*(.+)", why)
    if not m:
        return None
    topic = re.sub(r"\s*\(.*?\)\s*$", "", m.group(1)).strip().rstrip(")").strip()
    if not topic or len(topic) > 48:
        return None
    return topic


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
            # Per-scout topics: the distinct topics THIS scout recently surfaced
            # (from its own source.discovered events), not the global agenda.
            searched = await conn.fetch(
                "SELECT payload->'source'->>'why' AS why, emitted_at FROM events "
                "WHERE event_type = 'source.discovered' "
                "AND payload->'source'->>'source_kind' = $1 "
                "ORDER BY emitted_at DESC LIMIT 80",
                kind,
            )
    except asyncpg.UndefinedTableError:
        return {"status": "planned", "source_kind": kind}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("scout panel (%s) failed: %s", kind, e)
        return {"status": "error", "error": str(e), "source_kind": kind}

    topics: list[str] = []
    seen_lower: set[str] = set()
    for r in searched:
        topic = _parse_topic(r["why"])
        if topic and topic.lower() not in seen_lower:
            seen_lower.add(topic.lower())
            topics.append(topic)
        if len(topics) >= 8:
            break
    searched_at = searched[0]["emitted_at"].isoformat() if searched and searched[0]["emitted_at"] else None

    return {
        "status": "ok",
        "source_kind": kind,
        "in_corpus": in_corpus,
        "added_today": added_today,
        "last_searched": {"topics": topics, "at": searched_at},
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
async def gate_panel(request: Request, kind: str | None = None) -> dict:
    """The intake gate: who was admitted and who was turned away, with reasons —
    TRUST blocks (mimir.ingest_blocked) + QUALITY rejections
    (library.ingest_rejected), plus today's tallies and a recent-admitted list.

    With `kind` (a scout source kind), the gate is SCOPED to that scout's sources
    — what it surfaced and how it fared. No kind = the full gate (all sources)."""
    kind = (kind or "").strip().lower() or None
    if kind and kind not in _SCOUT_KINDS:
        return {"status": "error", "error": f"unknown kind {kind!r}"}
    # Optional " AND <col> = $1" filters, applied only when scoped to a kind.
    args = [kind] if kind else []
    doc_f = " AND d.source_kind = $1" if kind else ""
    docs_f = " AND source_kind = $1" if kind else ""
    rej_f = " AND payload->>'source_kind' = $1" if kind else ""
    disc_f = " AND payload->'source'->>'source_kind' = $1" if kind else ""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            today = {
                "admitted": await conn.fetchval(
                    "SELECT COUNT(*) FROM documents d WHERE d.status='certified'"
                    + doc_f
                    + " AND COALESCE(d.certified_at, d.ingested_at) >= date_trunc('day', now())",
                    *args,
                )
                or 0,
                "blocked_trust": await conn.fetchval(
                    "SELECT COUNT(*) FROM events e JOIN documents d ON d.id = e.target_id "
                    "WHERE e.event_type='mimir.ingest_blocked'" + doc_f + " AND e.emitted_at >= date_trunc('day', now())",
                    *args,
                )
                or 0,
                "rejected_quality": await conn.fetchval(
                    "SELECT COUNT(*) FROM events WHERE event_type='library.ingest_rejected'"
                    + rej_f
                    + " AND emitted_at >= date_trunc('day', now())",
                    *args,
                )
                or 0,
                "discovered": await conn.fetchval(
                    "SELECT COUNT(*) FROM events WHERE event_type='source.discovered'"
                    + disc_f
                    + " AND emitted_at >= date_trunc('day', now())",
                    *args,
                )
                or 0,
            }
            in_corpus = await conn.fetchval("SELECT COUNT(*) FROM documents d WHERE TRUE" + doc_f, *args) or 0
            quarantined = (
                await conn.fetchval("SELECT COUNT(*) FROM documents WHERE status='blocked'" + docs_f, *args) or 0
            )
            blocked = await conn.fetch(
                "SELECT e.emitted_at, e.payload, d.title, d.source_kind, d.source_url "
                "FROM events e JOIN documents d ON d.id = e.target_id "
                "WHERE e.event_type='mimir.ingest_blocked'" + doc_f + " ORDER BY e.emitted_at DESC LIMIT 15",
                *args,
            )
            rejected = await conn.fetch(
                "SELECT emitted_at, payload FROM events WHERE event_type='library.ingest_rejected'"
                + rej_f
                + " ORDER BY emitted_at DESC LIMIT 15",
                *args,
            )
            admitted = await conn.fetch(
                "SELECT title, source_kind, arxiv_id, canonical_key, trust_tier, "
                "COALESCE(certified_at, ingested_at) at FROM documents d WHERE status='certified'"
                + doc_f
                + " ORDER BY COALESCE(certified_at, ingested_at) DESC NULLS LAST LIMIT 8",
                *args,
            )
    except asyncpg.UndefinedTableError:
        return {"status": "planned"}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("gate panel (kind=%s) failed: %s", kind, e)
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
        "scope": kind or "all",
        "in_corpus": in_corpus,
        "today": today,
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


# Each displayable metric maps to the bus event whose hourly count is the trend.
# 'certified' is an alias of document.ingested (Mimir certifies on ingest).
_METRIC_EVENT = {
    "discovered": "source.discovered",
    "parsed": "document.parsed",
    "ingested": "document.ingested",
    "certified": "document.ingested",
    "quarantined": "mimir.ingest_blocked",
}
# Document-centric metrics scope by kind via a join on target_id (the document);
# 'discovered' scopes via the nested payload source kind (like the gate panel).
_DOC_JOIN_METRICS = {"parsed", "ingested", "certified", "quarantined"}


@router.get("/timeseries")
async def knowledge_timeseries(
    request: Request,
    metric: str = "ingested",
    kind: str | None = None,
    bucket: str = "hour",
    points: int = 24,
) -> dict:
    """Gap-filled activity series from the events table — backs the scout
    sparklines and the KPI/storage 24h deltas. Buckets (hour|day) are
    generated continuously so empty hours render as zeros, not gaps. Optional
    `kind` scopes to one scout's sources. Real data; degrades to
    status='error'/[] rather than 500-ing the dashboard.

        GET /knowledge/timeseries?metric=ingested&kind=arxiv&bucket=hour&points=24
    """
    metric = (metric or "").strip().lower()
    if metric not in _METRIC_EVENT:
        return {"status": "error", "error": f"unknown metric {metric!r}", "points": []}
    bucket = (bucket or "hour").strip().lower()
    if bucket not in ("hour", "day"):
        return {"status": "error", "error": f"unknown bucket {bucket!r}", "points": []}
    kind = (kind or "").strip().lower() or None
    if kind and kind not in _SCOUT_KINDS:
        return {"status": "error", "error": f"unknown kind {kind!r}", "points": []}

    n = min(max(points, 2), 168)
    event_type = _METRIC_EVENT[metric]
    unit = bucket  # validated to 'hour' | 'day' — safe to inline below

    join = ""
    kind_clause = ""
    args: list = []
    if kind:
        if metric in _DOC_JOIN_METRICS:
            join = "JOIN documents d ON d.id = e.target_id"
            kind_clause = "AND d.source_kind = $1"
        else:  # discovered — kind lives in the event payload
            kind_clause = "AND e.payload->'source'->>'source_kind' = $1"
        args.append(kind)

    # Only `kind` is user-controlled (bound as $1); metric/bucket/points are
    # validated against allowlists / clamped, so inlining them is injection-safe.
    sql = f"""
        WITH buckets AS (
            SELECT generate_series(
                date_trunc('{unit}', now()) - ({n} - 1) * interval '1 {unit}',
                date_trunc('{unit}', now()),
                interval '1 {unit}'
            ) AS t
        ),
        hits AS (
            SELECT date_trunc('{unit}', e.emitted_at) AS t, COUNT(*) AS c
            FROM events e {join}
            WHERE e.event_type = '{event_type}'
              AND e.emitted_at >= date_trunc('{unit}', now()) - ({n} - 1) * interval '1 {unit}'
              {kind_clause}
            GROUP BY 1
        )
        SELECT b.t AS t, COALESCE(h.c, 0)::int AS value
        FROM buckets b LEFT JOIN hits h ON h.t = b.t
        ORDER BY b.t
    """
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except asyncpg.UndefinedTableError:
        return {"status": "planned", "metric": metric, "kind": kind, "bucket": bucket, "points": []}
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("knowledge timeseries (metric=%s kind=%s) failed: %s", metric, kind, e)
        return {"status": "error", "error": str(e), "metric": metric, "kind": kind, "bucket": bucket, "points": []}
    return {
        "status": "ok",
        "metric": metric,
        "kind": kind,
        "bucket": bucket,
        "points": [{"t": r["t"].isoformat(), "value": r["value"]} for r in rows],
    }
