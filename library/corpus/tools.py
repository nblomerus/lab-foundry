"""
LabFoundry corpus retrieval — the RAG read path over the pgvector corpus (§6).

A SEPARATE failure domain from labfoundry_knowledge (which owns the Neo4j driver
and only Cypher): this module owns the asyncpg pgvector pool + an Ollama embedder.

Provides:
- A lazy-singleton asyncpg pool whose conn-init REGISTERS THE PGVECTOR CODEC
  (binding list[float] as a vector($n) param fails without it) and SETs
  hnsw.ef_search.
- An Embedder calling the Ollama embed endpoint (guards both modern /api/embed
  and legacy /api/embeddings shapes), routed through the shared GPULock when
  in-process (else a local Semaphore(1)).
- Public async tools (exposed over MCP AND imported directly in-process — the
  real integration path per §6: TOOLS_BY_AGENT tool_names is dead code):
    corpus_search · build_context · corpus_get_document · list_datasets
- An internal _search_by_vector(vec, ...) that corpus_search calls AFTER embedding
  — so tests can inject a known query vector and bypass the Embedder/Ollama.

Internal helpers (_get_pool, _search_by_vector, Embedder, _embedder) are NOT
exposed over MCP; only the four public tools are registered in server.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import UTC, datetime

import asyncpg
import httpx
import pgvector.asyncpg
from pydantic import BaseModel

log = logging.getLogger(__name__)

# =========================================================================
# Config (mirror api/harness: DATABASE_URL is a hard subscript; OLLAMA_URL has
# a default). EMBED_MODEL is net-new — introduced here with a 768-dim default.
# =========================================================================

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")  # 768-dim
EMBED_DIM = 768
HNSW_EF_SEARCH = int(os.environ.get("HNSW_EF_SEARCH", "40"))

# Rerank weights — module constants (§6). Trust at 0.30 must NOT turn retrieval
# into a pure authority filter; semantic similarity stays dominant at 0.60.
W_SIM = 0.60
W_TRUST = 0.30
W_RECENCY = 0.10
RECENCY_HALFLIFE_DAYS = 180.0

# trust_w lookup keyed by tier name. A missing tier (write race) defaults to 0.4
# — treated as unverified, never as certified (§6).
TRUST_WEIGHT: dict[str, float] = {
    "quarantined": 0.0,
    "user_asserted": 0.3,
    "web_unknown": 0.4,
    "web_reputable": 0.6,
    "official_repo": 0.75,
    "preprint": 0.85,
    "peer_reviewed": 1.0,
}
DEFAULT_TRUST_W = 0.4


# =========================================================================
# Pydantic models (§6)
# =========================================================================


class RetrievedChunk(BaseModel):
    chunk_id: int
    document_id: int
    ordinal: int
    text: str
    token_count: int | None = None
    kind: str
    title: str | None = None
    source_url: str | None = None
    trust_tier: str
    ingested_at: datetime | None = None
    # scoring breakdown (surfaced for /trace debugging)
    distance: float
    sim: float
    trust_w: float
    recency: float
    score: float


class ProvenanceSpan(BaseModel):
    char_start: int
    char_end: int
    document_id: int
    chunk_id: int
    ordinal: int
    title: str | None = None
    source_url: str | None = None
    trust_tier: str
    score: float


class ContextBlock(BaseModel):
    text: str
    spans: list[ProvenanceSpan]
    total_tokens: int
    dropped: int


class DocumentDetail(BaseModel):
    id: int
    kind: str
    title: str | None = None
    authors: list[str] = []
    source_kind: str
    source_url: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    published_at: datetime | None = None
    ingested_at: datetime | None = None
    license: str | None = None
    status: str
    trust_tier: str
    trust_state: str
    queryable: bool
    chunk_count: int


class DatasetRow(BaseModel):
    id: int
    name: str
    url: str | None = None
    modality: str | None = None
    task: str | None = None
    size: str | None = None
    license: str | None = None
    notes: str | None = None
    document_id: int | None = None


# =========================================================================
# Lazy singleton pool (mirror labfoundry_knowledge/tools.py:32-45) — with the
# CRITICAL pgvector codec registration that the api JSONB-only _init_conn lacks.
# =========================================================================

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _init_conn(conn: asyncpg.Connection) -> None:
    """
    Conn init for the corpus pool.

    ⚠️ Review fix (§6): asyncpg has NO native `vector` type, so binding a
    list[float] as `$1` in `ORDER BY c.embedding <=> $1` fails at runtime unless
    pgvector.asyncpg.register_vector(conn) runs here. Copying api/main.py's
    jsonb-only _init_conn is INSUFFICIENT. We also keep the jsonb codec so
    `documents.provenance` round-trips as a dict, and SET the HNSW recall knob
    per-connection.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await pgvector.asyncpg.register_vector(conn)
    await conn.execute(f"SET hnsw.ef_search = {HNSW_EF_SEARCH}")


async def _get_pool() -> asyncpg.Pool:
    """Lazy-initialize the corpus asyncpg pool on first call. Thread-safe."""
    global _pool
    async with _pool_lock:
        if _pool is None:
            db_url = os.environ["DATABASE_URL"]
            _pool = await asyncpg.create_pool(
                db_url,
                min_size=2,
                max_size=10,
                init=_init_conn,
            )
            log.info("labfoundry_corpus: pgvector pool initialized")
    return _pool


# =========================================================================
# Embedder — Ollama embed, lock-aware, dim-checked
# =========================================================================


class Embedder:
    """
    Embeds query text via Ollama. Guards BOTH endpoint shapes (§6 review fix):
      modern  POST /api/embed       {model, input}  -> data['embeddings'][0]
      legacy  POST /api/embeddings   {model, prompt} -> data['embedding']
    A wrong shape returns 200 with different JSON and a wrong-length vector, so we
    assert dim == 768 after extracting.

    VRAM: query embeds must respect the GPU, not just budget (§6). We prefer the
    shared in-process GPULock when importable; otherwise a local Semaphore(1).
    CAVEAT (§6): GPULock uses process-local asyncio primitives — importing it in a
    SEPARATE MCP-server process yields a different instance, so cross-process VRAM
    serialization is NOT guaranteed. In-process direct import (the real path) does
    serialize within the same event loop.
    """

    def __init__(self, ollama_url: str = OLLAMA_URL, model: str = EMBED_MODEL):
        self.ollama_url = ollama_url
        self.model = model
        self._http = httpx.AsyncClient(timeout=120.0)
        self._gpu_lock = None
        self._sem: asyncio.Semaphore | None = None
        try:
            from harness.router import GPULock

            self._gpu_lock = GPULock()
        except Exception:
            # Not running in the harness process (or import failed). Fall back to
            # a local single-permit semaphore; see the cross-process caveat above.
            self._sem = asyncio.Semaphore(1)

    async def embed(self, text: str) -> list[float]:
        if self._gpu_lock is not None:
            async with self._gpu_lock.acquire(self.model):
                vec = await self._embed_raw(text)
        else:
            assert self._sem is not None
            async with self._sem:
                vec = await self._embed_raw(text)
        if len(vec) != EMBED_DIM:
            raise ValueError(
                f"embed dim mismatch: got {len(vec)}, expected {EMBED_DIM} "
                f"(model={self.model}; wrong endpoint shape or wrong model?)"
            )
        return vec

    async def _embed_raw(self, text: str) -> list[float]:
        # Try the modern endpoint first; fall back to legacy on 404.
        resp = await self._http.post(
            f"{self.ollama_url}/api/embed",
            json={"model": self.model, "input": text},
        )
        if resp.status_code == 404:
            resp = await self._http.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.model, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return list(data["embedding"])  # legacy shape
        resp.raise_for_status()
        data = resp.json()
        if "embeddings" in data:  # modern shape
            return list(data["embeddings"][0])
        if "embedding" in data:  # some builds return legacy key on /api/embed
            return list(data["embedding"])
        raise ValueError(f"unexpected Ollama embed response keys: {list(data.keys())}")

    async def close(self) -> None:
        await self._http.aclose()


_embedder: Embedder | None = None
_embedder_lock = asyncio.Lock()


async def _get_embedder() -> Embedder:
    global _embedder
    async with _embedder_lock:
        if _embedder is None:
            _embedder = Embedder()
    return _embedder


# =========================================================================
# Rerank helpers
# =========================================================================


def _recency_weight(ingested_at: datetime | None) -> float:
    """exp(-age_days / halflife). Missing date -> neutral-ish 0.0 recency."""
    if ingested_at is None:
        return 0.0
    now = datetime.now(UTC)
    ts = ingested_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return math.exp(-age_days / RECENCY_HALFLIFE_DAYS)


def _score_row(
    distance: float, trust_tier: str | None, ingested_at: datetime | None
) -> tuple[float, float, float, float]:
    """Return (sim, trust_w, recency, score) for one candidate row."""
    # cosine distance in [0,2]; similarity = 1 - distance, clamped to [0,1].
    sim = max(0.0, min(1.0, 1.0 - float(distance)))
    trust_w = TRUST_WEIGHT.get(trust_tier or "", DEFAULT_TRUST_W)
    recency = _recency_weight(ingested_at)
    score = W_SIM * sim + W_TRUST * trust_w + W_RECENCY * recency
    return sim, trust_w, recency, score


# =========================================================================
# Retrieval core (the §6 SQL) — split so tests can inject a query vector and
# bypass the Embedder/Ollama entirely (CRITICAL: keeps the test CI-runnable).
# =========================================================================


async def _search_by_vector(
    vec: list[float],
    k: int = 8,
    *,
    kind: str | None = None,
    min_trust: str | None = None,
) -> list[RetrievedChunk]:
    """
    The ANN candidate pool + Python rerank, over an ALREADY-EMBEDDED query vector.
    corpus_search calls this after embedding; tests call it directly.

    Candidate pool N = max(4*k, 32); enforces the §4 canonical trust floor:
      trust_rank(d.trust_tier) >= trust_rank($min_trust)
      AND d.trust_state NOT IN ('quarantined','decayed')
      AND d.status='certified' AND d.queryable
    `min_trust=NULL` -> floor of 'quarantined' (rank 0), i.e. no extra tier filter
    beyond the certified/queryable/non-decayed gate.
    """
    n = max(4 * k, 32)
    floor = min_trust if min_trust is not None else "quarantined"
    sql = """
        SELECT c.id, c.document_id, c.ordinal, c.text, c.token_count,
               d.kind, d.title, d.source_url, d.trust_tier, d.ingested_at,
               (c.embedding <=> $1) AS distance
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE ($2::text IS NULL OR d.kind = $2::document_kind)
          AND d.status = 'certified' AND d.queryable
          AND trust_rank(d.trust_tier) >= trust_rank($3::trust_tier)
          AND d.trust_state NOT IN ('quarantined','decayed')
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> $1
        LIMIT $4
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, vec, kind, floor, n)

    out: list[RetrievedChunk] = []
    for r in rows:
        trust_tier = r["trust_tier"]
        sim, trust_w, recency, score = _score_row(r["distance"], trust_tier, r["ingested_at"])
        out.append(
            RetrievedChunk(
                chunk_id=r["id"],
                document_id=r["document_id"],
                ordinal=r["ordinal"],
                text=r["text"],
                token_count=r["token_count"],
                kind=r["kind"],
                title=r["title"],
                source_url=r["source_url"],
                trust_tier=trust_tier,
                ingested_at=r["ingested_at"],
                distance=float(r["distance"]),
                sim=sim,
                trust_w=trust_w,
                recency=recency,
                score=score,
            )
        )
    # Python rerank over the N candidates, then truncate to k.
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:k]


# =========================================================================
# Public tools (registered over MCP in server.py AND imported in-process)
# =========================================================================


async def corpus_search(
    query: str,
    k: int = 8,
    *,
    kind: str | None = None,
    min_trust: str | None = None,
    kg_expand: bool = False,
) -> list[RetrievedChunk]:
    """
    Semantic search over the certified corpus. Pipeline (§6):
      1. embed query -> 768-d vec via Ollama (GPULock-aware).
      2. ANN over chunks (candidate pool N=max(4k,32)) honoring the trust floor.
      3. Python rerank: 0.60*sim + 0.30*trust_w + 0.10*recency.
    `kg_expand` is accepted for signature stability but is OFF by default and
    NOT yet wired (it would cost a Neo4j hop — deferred past Phase 1).
    """
    embedder = await _get_embedder()
    vec = await embedder.embed(query)
    return await _search_by_vector(vec, k, kind=kind, min_trust=min_trust)


async def build_context(
    query: str,
    *,
    k: int = 12,
    max_tokens: int = 3000,
    kind: str | None = None,
    min_trust: str | None = None,
) -> ContextBlock:
    """
    Greedily fill WHOLE chunks (never mid-chunk) until `max_tokens`, emitting each
    as `[#i] {text}\\n` and recording its (char_start, char_end). Deterministic —
    no LLM call inside. The `[#i]` markers let an LLM cite by index; spans[i]
    resolves index -> chunk_id -> document_id for a downstream CITES edge (§1/§2).

    Degenerate case: a single chunk exceeding max_tokens alone returns empty text
    with dropped>0; the caller must handle it. Token counting uses the chunk's
    stored token_count when present, else a cheap len/4 estimate.
    """
    chunks = await corpus_search(query, k, kind=kind, min_trust=min_trust)

    parts: list[str] = []
    spans: list[ProvenanceSpan] = []
    total_tokens = 0
    dropped = 0
    cursor = 0

    for i, c in enumerate(chunks):
        tok = c.token_count if c.token_count else max(1, len(c.text) // 4)
        if total_tokens + tok > max_tokens:
            dropped += 1
            continue
        marker = f"[#{i}] "
        segment = f"{marker}{c.text}\n"
        # char span covers the chunk text itself (excludes the marker prefix and
        # the trailing newline) so a downstream citation maps cleanly to the chunk.
        text_start = cursor + len(marker)
        text_end = text_start + len(c.text)
        parts.append(segment)
        spans.append(
            ProvenanceSpan(
                char_start=text_start,
                char_end=text_end,
                document_id=c.document_id,
                chunk_id=c.chunk_id,
                ordinal=c.ordinal,
                title=c.title,
                source_url=c.source_url,
                trust_tier=c.trust_tier,
                score=c.score,
            )
        )
        cursor += len(segment)
        total_tokens += tok

    return ContextBlock(
        text="".join(parts),
        spans=spans,
        total_tokens=total_tokens,
        dropped=dropped,
    )


async def corpus_get_document(document_id: int) -> DocumentDetail | None:
    """Fetch one document's registry row + its chunk count. None if not found."""
    pool = await _get_pool()
    sql = """
        SELECT d.id, d.kind, d.title, d.authors, d.source_kind, d.source_url,
               d.doi, d.arxiv_id, d.published_at, d.ingested_at, d.license,
               d.status, d.trust_tier, d.trust_state, d.queryable,
               (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
        FROM documents d
        WHERE d.id = $1
    """
    async with pool.acquire() as conn:
        r = await conn.fetchrow(sql, document_id)
    if r is None:
        return None
    return DocumentDetail(
        id=r["id"],
        kind=r["kind"],
        title=r["title"],
        authors=list(r["authors"] or []),
        source_kind=r["source_kind"],
        source_url=r["source_url"],
        doi=r["doi"],
        arxiv_id=r["arxiv_id"],
        published_at=r["published_at"],
        ingested_at=r["ingested_at"],
        license=r["license"],
        status=r["status"],
        trust_tier=r["trust_tier"],
        trust_state=r["trust_state"],
        queryable=r["queryable"],
        chunk_count=r["chunk_count"],
    )


async def list_datasets(task: str | None = None) -> list[DatasetRow]:
    """List dataset registry rows, optionally filtered by `task`."""
    pool = await _get_pool()
    sql = """
        SELECT id, name, url, modality, task, size, license, notes, document_id
        FROM datasets
        WHERE ($1::text IS NULL OR task = $1)
        ORDER BY name
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, task)
    return [
        DatasetRow(
            id=r["id"],
            name=r["name"],
            url=r["url"],
            modality=r["modality"],
            task=r["task"],
            size=r["size"],
            license=r["license"],
            notes=r["notes"],
            document_id=r["document_id"],
        )
        for r in rows
    ]
