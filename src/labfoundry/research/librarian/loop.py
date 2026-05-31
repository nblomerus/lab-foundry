"""
loop.py — the Librarian ingest loop (MIMIR_WARDEN_SCOPE.md §3).

The Librarian is the corpus DOER. Phase 2 keeps the whole ingest pipeline
DETERMINISTIC — there are NO LLM recipes here:

  * fetch  — resolve full text via the existing `fetcher` read path.
  * parse  — `parser.parse_paper` (vendored rag-bench, heuristic, no LLM).
  * chunk  — `chunker.PaperChunker().plan` (vendored rag-bench, tokenizer math).
  * embed  — the SAME embedder the corpus read path uses
             (`labfoundry_corpus.tools._get_embedder`); no new embedder.
  * KG     — `merge_paper` from the parsed metadata only (best-effort, swallowed).

THE MIMIR GATE — why the loop is split into two phases
------------------------------------------------------
The Librarian governs NOTHING. It must "never self-certify". That invariant is
encoded MECHANICALLY by splitting the loop at the trust gate:

  PHASE A  (run on `source.discovered`):
      fetch -> parse -> chunk-plan -> upsert `documents` row (trust columns at
      their DB defaults: status='quarantined', trust_state='provisional') ->
      stage the chunk plan (text only, NO vectors) -> emit `document.parsed`.
      Phase A costs ONE cheap pass and then STOPS, awaiting Mimir.

  PHASE B  (run ONLY on `mimir.ingest_approved`):
      embed the staged chunks -> write vectors -> best-effort MERGE the KG
      Paper node -> flip `documents.queryable` -> emit `document.ingested`.

Making phase B a SEPARATE invocation triggered by Mimir's event turns the trust
gate into a hard control-flow boundary, not a politeness convention: a blocked
source costs one parse pass, never the (much larger) embed pass.

Mimir (Phase 3) does not exist yet. The dev escape `LIBRARIAN_AUTO_APPROVE`
(env, default OFF) lets phase A emit `mimir.ingest_approved` itself so the
pipeline can run end-to-end before Mimir lands. The emission lives in the
handler (`handlers/librarian.py`), not here, so the loop stays a pure doer.
"""

from __future__ import annotations

import hashlib
import logging

from labfoundry.research.fetcher import search_arxiv, web_fetch
from labfoundry.research.librarian.chunker import PaperChunker
from labfoundry.research.librarian.parser import parse_paper
from labfoundry.research.librarian.scouts import SourceDescriptor

log = logging.getLogger(__name__)


# Below this many characters the ar5iv full text is almost certainly a stub /
# challenge page (ar5iv occasionally has no HTML rendering for very new papers),
# so we fall back to the arXiv abstract rather than ingest a near-empty body.
_MIN_FULLTEXT_CHARS = 1_000

# Embed in batches so a 60-chunk paper doesn't open 60 concurrent HTTP calls.
# The embedder itself serializes on the GPULock; this just bounds how much we
# pull into memory / how big a single failure is.
_EMBED_BATCH = 32


# -------------------------------------------------------------------------
# Source normalization
# -------------------------------------------------------------------------


def _as_descriptor(source: dict | SourceDescriptor) -> SourceDescriptor:
    """Accept either a SourceDescriptor or its dict form (the event payload
    shape) and return a SourceDescriptor."""
    if isinstance(source, SourceDescriptor):
        return source
    return SourceDescriptor(**source)


# -------------------------------------------------------------------------
# Full-text resolution (deterministic; no LLM)
# -------------------------------------------------------------------------


async def _resolve_arxiv_fulltext(
    desc: SourceDescriptor,
    state,
) -> tuple[str, str | None]:
    """Resolve the best available text for an arXiv source.

    Strategy:
      1. fetch the ar5iv HTML rendering (full paper body) via the read path;
      2. if that yields too little (ar5iv miss / stub), fall back to the arXiv
         abstract — either one passed inline on the descriptor (`why` is a note,
         not the abstract, so we re-query) or re-queried by id via search_arxiv.

    Returns (text, ar5iv_url). `ar5iv_url` is the URL we actually fetched the
    body from (used as the document's source_url when the full body landed).
    """
    arxiv_id = desc.arxiv_id or desc.canonical_key
    ar5iv_url = f"https://ar5iv.org/abs/{arxiv_id}"

    text = ""
    page = await web_fetch(ar5iv_url, state)
    if page is not None and page.content and page.content.strip():
        text = page.content.strip()

    if len(text) >= _MIN_FULLTEXT_CHARS:
        return text, ar5iv_url

    # Fallback: the abstract. Re-query arXiv by id (best-effort).
    log.info(
        "librarian: ar5iv full text for %s too short (%d chars) — falling back to abstract",
        arxiv_id,
        len(text),
    )
    try:
        results = await search_arxiv(f"id:{arxiv_id}", max_results=1)
    except Exception as e:  # noqa: BLE001 — fallback is best-effort
        log.warning("librarian: abstract fallback search_arxiv(%s) failed: %s", arxiv_id, e)
        results = []

    if results and results[0].abstract.strip():
        abstract = results[0].abstract.strip()
        # Prefer the abstract only if it beats whatever ar5iv gave us.
        if len(abstract) > len(text):
            return abstract, ar5iv_url

    return text, ar5iv_url


async def _resolve_fulltext(
    desc: SourceDescriptor,
    state,
) -> tuple[str, str | None]:
    """Dispatch full-text resolution by source_kind. Returns (text, fetched_url).

    For non-arXiv sources we fall back to a plain `web_fetch` of the descriptor
    url (covers source_kind=='web' and the test path)."""
    if desc.source_kind == "arxiv":
        return await _resolve_arxiv_fulltext(desc, state)

    if desc.url:
        page = await web_fetch(desc.url, state)
        if page is not None and page.content and page.content.strip():
            return page.content.strip(), desc.url

    return "", desc.url


# -------------------------------------------------------------------------
# PHASE A — fetch -> parse -> chunk-plan -> persist (provisional), STOP
# -------------------------------------------------------------------------


async def run_ingest_phase_a(
    source: dict | SourceDescriptor,
    state,
    *,
    dispatcher=None,
) -> dict:
    """Run phase A of ingest for one discovered source.

    Resolves full text, parses + chunk-plans deterministically, upserts the
    `documents` row (trust columns left at their quarantined/provisional DB
    defaults — Mimir owns them), stages the chunk plan WITHOUT vectors, and
    emits `document.parsed`. Then STOPS, awaiting `mimir.ingest_approved`.

    Returns one of:
      {"skipped": True, "reason": ...}                 — nothing fetchable
      {"document_id": id, "deduped": True}             — already ingested
      {"document_id": id, "n_chunks": n, "awaiting": "mimir"}  — staged, gated
    """
    desc = _as_descriptor(source)

    text, fetched_url = await _resolve_fulltext(desc, state)
    if not text or not text.strip():
        log.info("librarian phase A: no fetchable text for %s/%s", desc.source_kind, desc.canonical_key)
        return {"skipped": True, "reason": "empty/blocked"}

    # Parse (deterministic, no LLM).
    parsed = parse_paper(
        text,
        arxiv_id=desc.arxiv_id,
        doi=desc.doi,
        title=desc.title,
        url=fetched_url or desc.url,
    )

    # Chunk-plan (deterministic, no LLM).
    plan = PaperChunker().plan(parsed)

    # content_hash is sha256 of the raw resolved text — the exact-bytes dedupe
    # backstop alongside the (source_kind, canonical_key) upsert key.
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    doc_id, is_new = await state.upsert_document(
        kind=desc.kind,
        source_kind=desc.source_kind,
        canonical_key=desc.canonical_key,
        title=parsed.title,
        authors=parsed.authors,
        source_url=fetched_url or desc.url,
        doi=parsed.doi,
        arxiv_id=parsed.arxiv_id,
        raw_uri=desc.url or fetched_url,
        content_hash=content_hash,
    )

    if not is_new:
        log.info(
            "librarian phase A: %s/%s already ingested as doc %s — deduped",
            desc.source_kind,
            desc.canonical_key,
            doc_id,
        )
        return {"document_id": doc_id, "deduped": True}

    n_inserted = await state.stage_chunk_plan(doc_id, plan)

    await state.emit_corpus_event(
        "document.parsed",
        target_type="document",
        target_id=doc_id,
        payload={
            "kind": desc.kind,
            "n_chunks": len(plan),
            "title": parsed.title,
            "url": fetched_url or desc.url,
        },
        dedup_key=f"parsed-{doc_id}",
    )

    log.info(
        "librarian phase A: doc %s staged %d/%d chunks — awaiting Mimir",
        doc_id,
        n_inserted,
        len(plan),
    )
    return {"document_id": doc_id, "n_chunks": len(plan), "awaiting": "mimir"}


# -------------------------------------------------------------------------
# PHASE B — embed -> write vectors -> KG -> flip queryable (Mimir-approved only)
# -------------------------------------------------------------------------


async def _embed_pending(plan: list[dict]) -> tuple[list[dict], int, int]:
    """Embed the chunks in `plan` that lack a vector, using the corpus read
    path's embedder (no new embedder). Returns
    (rows_for_set_chunk_embeddings, embedded_count, failed_count).

    Embed errors are NON-FATAL: a failed chunk is simply skipped (its row stays
    NULL) and logged, so one flaky embed call doesn't sink the whole document.
    """
    from labfoundry.mcp_servers.labfoundry_corpus.tools import EMBED_MODEL, _get_embedder

    pending = [c for c in plan if not c.get("has_embedding")]
    if not pending:
        return [], 0, 0

    embedder = await _get_embedder()
    rows: list[dict] = []
    failed = 0
    for start in range(0, len(pending), _EMBED_BATCH):
        batch = pending[start : start + _EMBED_BATCH]
        for c in batch:
            try:
                vec = await embedder.embed(c["text"])
            except Exception as e:  # noqa: BLE001 — per-chunk embed failure is non-fatal
                failed += 1
                log.warning("librarian phase B: embed failed for chunk ord %s: %s", c.get("ordinal"), e)
                continue
            rows.append(
                {
                    "ordinal": c["ordinal"],
                    "content_hash": c["content_hash"],
                    "embedding": vec,
                    "embed_model": EMBED_MODEL,
                }
            )
    return rows, len(rows), failed


async def run_ingest_phase_b(document_id: int, state) -> dict:
    """Run phase B of ingest for one Mimir-approved document.

    Embeds the staged chunks lacking a vector (via the corpus read path's
    embedder), writes the vectors, best-effort MERGEs the KG Paper node from the
    document row metadata (swallowed — Neo4j is non-fatal), flips
    `documents.queryable`, and emits `document.ingested`.

    Returns one of:
      {"skipped": True, "reason": ...}                       — missing/blocked
      {"document_id": id, "queryable": True, "embedded": N}  — ingested
    """
    doc = await state.get_document(document_id)
    if doc is None:
        log.info("librarian phase B: doc %s not found — skipping", document_id)
        return {"skipped": True, "reason": "not_found"}

    # Mimir blocked it: status='blocked' or trust_state quarantined/decayed.
    if doc.get("status") == "blocked" or doc.get("trust_state") in {"quarantined", "decayed"}:
        log.info(
            "librarian phase B: doc %s blocked by Mimir (status=%s, trust_state=%s) — skipping",
            document_id,
            doc.get("status"),
            doc.get("trust_state"),
        )
        return {"skipped": True, "reason": "blocked"}

    plan = await state.get_chunk_plan(document_id)
    rows, embedded, failed = await _embed_pending(plan)
    if rows:
        await state.set_chunk_embeddings(document_id, rows)

    # Best-effort KG MERGE from the parsed metadata already on the document row.
    # Swallowed: Neo4j is a read-optimized projection and may be unavailable.
    try:
        from labfoundry.mcp_servers.labfoundry_knowledge.tools import merge_paper

        await merge_paper(
            document_id,
            doi=doc.get("doi"),
            arxiv_id=doc.get("arxiv_id"),
            title=doc.get("title"),
            year=(doc.get("published_at").year if doc.get("published_at") else None),
            trust_tier=doc.get("trust_tier"),
            source_url=doc.get("source_url"),
            authors=list(doc.get("authors") or []),
        )
    except Exception:  # noqa: BLE001 — KG is best-effort, never blocks ingest
        log.exception("librarian phase B: merge_paper failed for doc %s — continuing", document_id)

    await state.set_document_queryable(document_id, True)

    await state.emit_corpus_event(
        "document.ingested",
        target_type="document",
        target_id=document_id,
        payload={
            "kind": doc.get("kind"),
            "n_chunks": len(plan),
            "embedded": embedded,
            "trust_tier": doc.get("trust_tier"),
        },
        dedup_key=f"ingested-{document_id}",
    )

    log.info(
        "librarian phase B: doc %s queryable — embedded %d chunks (%d failed)",
        document_id,
        embedded,
        failed,
    )
    return {"document_id": document_id, "queryable": True, "embedded": embedded}
