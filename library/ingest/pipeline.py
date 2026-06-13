"""
pipeline.py — the deterministic ingest tools Mimir calls (MIMIR_WARDEN_SCOPE §3).

Two pure-doer functions, NO LLM:

  stage_source(source, state)
      fetch -> parse -> chunk-plan -> upsert `documents` (trust columns left at
      their quarantined/provisional DB defaults — Mimir owns them) -> stage the
      chunk plan (text only, NO vectors) -> emit `document.parsed`. Cheap; runs
      BEFORE the trust decision so a blocked source never costs an embed pass.

  embed_and_finalize(document_id, state)
      embed the staged chunks -> write vectors -> best-effort MERGE the KG Paper
      node -> flip `documents.queryable` -> emit `document.ingested`. Runs only
      AFTER Mimir approves the document.

Mimir orchestrates the two with the trust gate between them:
    stage_source -> classify_trust -> (certify) -> embed_and_finalize
Splitting at the gate keeps "never spend the expensive embed on an untrusted
source" a mechanical control-flow fact, not a politeness convention. fetch /
parse / chunk / embed / KG are all deterministic — the only judgment (trust)
lives in classify_trust + Mimir, never here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import httpx

from library.graph.tools import merge_paper
from library.ingest.chunker import PaperChunker
from library.ingest.fetcher import USER_AGENT, search_arxiv, web_fetch
from library.ingest.parser import parse_paper
from library.ingest.quality import assess_quality
from library.ingest.scouts import SourceDescriptor

log = logging.getLogger(__name__)


# Below this many characters the ar5iv full text is almost certainly a stub /
# challenge page (ar5iv occasionally has no HTML rendering for very new papers),
# so we fall back to the arXiv abstract rather than ingest a near-empty body.
_MIN_FULLTEXT_CHARS = 1_000

_GITHUB_API = "https://api.github.com"
_HF_API = "https://huggingface.co"
_HF_DATASETS_SERVER = "https://datasets-server.huggingface.co"
_OPENML_API = "https://www.openml.org/api/v1/json"

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


def _source_target_id(canonical_key: str) -> int:
    """Stable positive bigint from a source's canonical_key (for event keys when
    there is no document id yet — e.g. a source rejected before staging)."""
    return int.from_bytes(hashlib.blake2b(canonical_key.encode(), digest_size=7).digest(), "big")


async def _emit_rejected(state, desc: SourceDescriptor, stage: str, reason: str) -> None:
    """Record a source TURNED AWAY at intake (quality gate / no usable content),
    so the entrance/gate view can show what was rejected and why. Deduped per
    source; telemetry-only, never raises into the ingest path."""
    try:
        await state.emit_corpus_event(
            "library.ingest_rejected",
            target_type="source",
            target_id=_source_target_id(desc.canonical_key),
            payload={
                "source_kind": desc.source_kind,
                "canonical_key": desc.canonical_key,
                "url": desc.url,
                "title": desc.title,
                "stage": stage,
                "reason": reason,
            },
            dedup_key=f"rejected-{stage}-{desc.source_kind}-{desc.canonical_key}",
        )
    except Exception:  # noqa: BLE001 — telemetry must never break ingest
        log.exception("ingest: failed to emit ingest_rejected for %s", desc.canonical_key)


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
        "ingest: ar5iv full text for %s too short (%d chars) — falling back to abstract",
        arxiv_id,
        len(text),
    )
    try:
        results = await search_arxiv(f"id:{arxiv_id}", max_results=1)
    except Exception as e:  # noqa: BLE001 — fallback is best-effort
        log.warning("ingest: abstract fallback search_arxiv(%s) failed: %s", arxiv_id, e)
        results = []

    if results and results[0].abstract.strip():
        abstract = results[0].abstract.strip()
        # Prefer the abstract only if it beats whatever ar5iv gave us.
        if len(abstract) > len(text):
            return abstract, ar5iv_url

    return text, ar5iv_url


async def _resolve_github_fulltext(desc: SourceDescriptor, state) -> tuple[str, str | None]:
    """Resolve a GitHub repo to real content via the API (NOT by scraping the
    JS-rendered repo page, which trafilatura mostly fails on): repo metadata
    (description, stars, language, topics, license) + the README markdown. We do
    NOT store the code itself — the README + metadata is the high-signal 'what is
    this and why does it matter' the lab actually needs."""
    repo = desc.canonical_key  # "owner/repo"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    meta: dict = {}
    readme = ""
    async with httpx.AsyncClient(timeout=12.0, headers=headers, follow_redirects=True) as client:
        try:
            r = await client.get(f"{_GITHUB_API}/repos/{repo}")
            if r.status_code == 200:
                meta = r.json()
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning("ingest: github repo meta %s failed: %s", repo, e)
        try:
            rr = await client.get(
                f"{_GITHUB_API}/repos/{repo}/readme",
                headers={**headers, "Accept": "application/vnd.github.raw+json"},
            )
            if rr.status_code == 200:
                readme = rr.text
        except Exception as e:  # noqa: BLE001 — many repos have no README
            log.info("ingest: github readme %s unavailable: %s", repo, e)

    parts = [f"# {repo}"]
    if meta.get("description"):
        parts.append(meta["description"])
    facts = []
    if meta.get("stargazers_count") is not None:
        facts.append(f"Stars: {meta['stargazers_count']}")
    if meta.get("forks_count") is not None:
        facts.append(f"Forks: {meta['forks_count']}")
    if meta.get("language"):
        facts.append(f"Primary language: {meta['language']}")
    if meta.get("topics"):
        facts.append("Topics: " + ", ".join(meta["topics"]))
    if (meta.get("license") or {}).get("spdx_id"):
        facts.append(f"License: {meta['license']['spdx_id']}")
    if meta.get("pushed_at"):
        facts.append(f"Last push: {meta['pushed_at']}")
    if facts:
        parts.append("\n".join(facts))
    if readme.strip():
        parts.append("## README\n" + readme.strip())
    return "\n\n".join(parts).strip(), (meta.get("html_url") or desc.url)


async def _resolve_dataset_fulltext(desc: SourceDescriptor, state) -> tuple[str, str | None]:
    """Resolve a HuggingFace dataset to a real landscape signal — NOT just the
    card blurb. Pulls hub metadata (task categories, modalities, languages, size,
    downloads, likes) and, via the datasets-server, the schema (feature names) +
    a couple of sample rows. That's what lets the lab understand WHAT data exists
    for a field and what's trending, rather than storing a marketing paragraph."""
    ds_id = desc.canonical_key
    meta: dict = {}
    features: list[str] = []
    sample_rows: list = []
    async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        try:
            r = await client.get(f"{_HF_API}/api/datasets/{ds_id}", params={"full": "true"})
            if r.status_code == 200:
                meta = r.json()
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning("ingest: hf dataset meta %s failed: %s", ds_id, e)
        try:
            sp = await client.get(f"{_HF_DATASETS_SERVER}/splits", params={"dataset": ds_id})
            splits = (sp.json() or {}).get("splits") or [] if sp.status_code == 200 else []
            if splits:
                cfg, split = splits[0].get("config"), splits[0].get("split")
                fr = await client.get(
                    f"{_HF_DATASETS_SERVER}/first-rows",
                    params={"dataset": ds_id, "config": cfg, "split": split},
                )
                if fr.status_code == 200:
                    body = fr.json()
                    features = [f.get("name") for f in (body.get("features") or []) if f.get("name")]
                    sample_rows = [row.get("row") for row in (body.get("rows") or [])[:2]]
        except Exception as e:  # noqa: BLE001 — many datasets aren't server-previewable
            log.info("ingest: hf datasets-server preview %s unavailable: %s", ds_id, e)

    tags = meta.get("tags") or []

    def _tagged(prefix: str) -> list[str]:
        return [t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith(prefix)]

    parts = [f"# Dataset: {ds_id}"]
    if meta.get("description"):
        parts.append(str(meta["description"]).strip())
    facts = []
    if _tagged("task_categories:"):
        facts.append("Tasks: " + ", ".join(_tagged("task_categories:")))
    if _tagged("modality:"):
        facts.append("Modalities: " + ", ".join(_tagged("modality:")))
    if _tagged("language:"):
        facts.append("Languages: " + ", ".join(_tagged("language:")))
    if _tagged("size_categories:"):
        facts.append("Size: " + ", ".join(_tagged("size_categories:")))
    if meta.get("downloads") is not None:
        facts.append(f"Downloads (30d): {meta['downloads']}")
    if meta.get("likes") is not None:
        facts.append(f"Likes: {meta['likes']}")
    if meta.get("lastModified"):
        facts.append(f"Last modified: {meta['lastModified']}")
    if facts:
        parts.append("\n".join(facts))
    if features:
        parts.append("Schema / features: " + ", ".join(features))
    if sample_rows:
        parts.append("Sample rows:\n" + json.dumps(sample_rows, default=str)[:1500])
    return "\n\n".join(parts).strip(), f"{_HF_API}/datasets/{ds_id}"


async def _resolve_openml_fulltext(desc: SourceDescriptor, state) -> tuple[str, str | None]:
    """Resolve an OpenML benchmark dataset via the v1 JSON API: its description +
    qualities (instances / features / classes / target). The landscape signal for
    classical-ML datasets — what it is and its shape — rather than the raw data."""
    did = desc.canonical_key.split(":", 1)[-1]
    meta: dict = {}
    quals: dict = {}
    async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        try:
            r = await client.get(f"{_OPENML_API}/data/{did}")
            if r.status_code == 200:
                meta = (r.json() or {}).get("data_set_description", {}) or {}
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning("ingest: openml data %s failed: %s", did, e)
        try:
            q = await client.get(f"{_OPENML_API}/data/qualities/{did}")
            if q.status_code == 200:
                for item in (q.json() or {}).get("data_qualities", {}).get("quality", []):
                    quals[item.get("name")] = item.get("value")
        except Exception as e:  # noqa: BLE001 — qualities are optional
            log.info("ingest: openml qualities %s unavailable: %s", did, e)

    parts = [f"# OpenML dataset: {meta.get('name', did)}"]
    if meta.get("description"):
        parts.append(str(meta["description"]).strip())
    facts = []
    for label, key in (
        ("Instances", "NumberOfInstances"),
        ("Features", "NumberOfFeatures"),
        ("Classes", "NumberOfClasses"),
        ("Missing values", "NumberOfMissingValues"),
    ):
        if quals.get(key):
            facts.append(f"{label}: {quals[key]}")
    if meta.get("default_target_attribute"):
        facts.append(f"Target: {meta['default_target_attribute']}")
    if meta.get("format"):
        facts.append(f"Format: {meta['format']}")
    if meta.get("upload_date"):
        facts.append(f"Uploaded: {meta['upload_date']}")
    if facts:
        parts.append("\n".join(facts))
    return "\n\n".join(parts).strip(), f"https://www.openml.org/d/{did}"


async def _resolve_fulltext(
    desc: SourceDescriptor,
    state,
) -> tuple[str, str | None]:
    """Dispatch full-text resolution by source_kind. Returns (text, fetched_url).

    arXiv -> ar5iv full body; github -> repo metadata + README via the API;
    dataset -> HF hub metadata + schema + sample rows; openml -> dataset
    description + qualities. Everything else (web, the test path) falls back to a
    plain `web_fetch` of the descriptor url."""
    if desc.source_kind == "arxiv":
        return await _resolve_arxiv_fulltext(desc, state)
    if desc.source_kind == "github":
        return await _resolve_github_fulltext(desc, state)
    if desc.source_kind == "dataset":
        return await _resolve_dataset_fulltext(desc, state)
    if desc.source_kind == "openml":
        return await _resolve_openml_fulltext(desc, state)

    # First-party lab outputs carry their text in-hand (passed to stage_source as
    # content_text), never fetched over the network. This branch is a safety net:
    # if a lab source ever reaches resolution without content_text, we return empty
    # (the quality gate then rejects it) rather than web-fetching a non-existent URL.
    if desc.source_kind in ("lab_experiment", "lab_dataset"):
        return "", desc.url

    if desc.url:
        page = await web_fetch(desc.url, state)
        if page is not None and page.content and page.content.strip():
            return page.content.strip(), desc.url

    return "", desc.url


# -------------------------------------------------------------------------
# STAGE — fetch -> parse -> chunk-plan -> persist (provisional), STOP
# -------------------------------------------------------------------------


async def stage_source(
    source: dict | SourceDescriptor,
    state,
    *,
    dispatcher=None,
    content_text: str | None = None,
) -> dict:
    """Stage one discovered source for ingest (the pre-trust pass).

    Resolves full text, parses + chunk-plans deterministically, upserts the
    `documents` row (trust columns left at their quarantined/provisional DB
    defaults — Mimir owns them), stages the chunk plan WITHOUT vectors, and
    emits `document.parsed`. Then STOPS — Mimir decides trust before any embed.

    `content_text` is the first-party content-in-hand path: when provided, the
    network resolution step is SKIPPED entirely and this text is used as the
    resolved body (fetched_url becomes the descriptor url, or a synthetic
    `lab://<source_kind>/<canonical_key>` when the source has no url). Everything
    downstream — quality gate, parse, chunk-plan, upsert, stage, emit — is
    identical to the fetched path.

    Returns one of:
      {"skipped": True, "reason": ...}                 — nothing fetchable
      {"document_id": id, "deduped": True}             — already ingested
      {"document_id": id, "n_chunks": n, "awaiting": "mimir"}  — staged, gated
    """
    desc = _as_descriptor(source)

    if content_text is not None:
        text = content_text
        fetched_url = desc.url or f"lab://{desc.source_kind}/{desc.canonical_key}"
    else:
        text, fetched_url = await _resolve_fulltext(desc, state)

    # QUALITY gate — applied to every source before we do any work. Thin stubs,
    # error/wall pages, and non-content remnants never become documents.
    quality = assess_quality(text, desc.source_kind)
    if not quality.ok:
        log.info(
            "ingest stage: quality gate rejected %s/%s — %s",
            desc.source_kind,
            desc.canonical_key,
            quality.reason,
        )
        await _emit_rejected(state, desc, "quality", quality.reason)
        return {"skipped": True, "reason": f"low_quality: {quality.reason}"}

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
    if not plan:
        # Text present but it chunked to nothing (too short / no recognizable
        # body). Don't create a hollow, never-retrievable document for it.
        log.info(
            "ingest stage: %s/%s produced 0 chunks — skipping (no retrievable content)",
            desc.source_kind,
            desc.canonical_key,
        )
        await _emit_rejected(state, desc, "quality", "no extractable content (0 chunks)")
        return {"skipped": True, "reason": "no_chunks"}

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
            "ingest stage: %s/%s already ingested as doc %s — deduped",
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
        "ingest stage: doc %s staged %d/%d chunks — awaiting Mimir",
        doc_id,
        n_inserted,
        len(plan),
    )
    return {"document_id": doc_id, "n_chunks": len(plan), "awaiting": "mimir"}


# -------------------------------------------------------------------------
# FINALIZE — embed -> write vectors -> KG -> flip queryable (post-approval)
# -------------------------------------------------------------------------


async def _embed_pending(plan: list[dict]) -> tuple[list[dict], int, int]:
    """Embed the chunks in `plan` that lack a vector, using the corpus read
    path's embedder (no new embedder). Returns
    (rows_for_set_chunk_embeddings, embedded_count, failed_count).

    Embed errors are NON-FATAL: a failed chunk is simply skipped (its row stays
    NULL) and logged, so one flaky embed call doesn't sink the whole document.
    """
    from library.corpus.tools import EMBED_MODEL, _get_embedder

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
                log.warning("ingest finalize: embed failed for chunk ord %s: %s", c.get("ordinal"), e)
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


async def embed_and_finalize(document_id: int, state) -> dict:
    """Finalize one Mimir-approved document (the post-trust pass).

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
        log.info("ingest finalize: doc %s not found — skipping", document_id)
        return {"skipped": True, "reason": "not_found"}

    # Mimir blocked it: status='blocked' or trust_state quarantined/decayed.
    if doc.get("status") == "blocked" or doc.get("trust_state") in {"quarantined", "decayed"}:
        log.info(
            "ingest finalize: doc %s blocked by Mimir (status=%s, trust_state=%s) — skipping",
            document_id,
            doc.get("status"),
            doc.get("trust_state"),
        )
        return {"skipped": True, "reason": "blocked"}

    plan = await state.get_chunk_plan(document_id)
    if not plan:
        # Nothing to embed — a hollow doc slipped through (legacy row, or a source
        # that resolved to text but chunked to nothing). Do NOT mark it queryable:
        # an empty, unretrievable row must not count as Library content.
        log.info("ingest finalize: doc %s has no chunks — leaving non-queryable", document_id)
        return {"skipped": True, "reason": "no_chunks"}
    rows, embedded, failed = await _embed_pending(plan)
    if rows:
        await state.set_chunk_embeddings(document_id, rows)
    if embedded == 0:
        log.info("ingest finalize: doc %s embedded 0 chunks — leaving non-queryable", document_id)
        return {"skipped": True, "reason": "no_embeddings"}

    # Best-effort KG MERGE from the parsed metadata already on the document row.
    # Swallowed: Neo4j is a read-optimized projection and may be unavailable.
    try:
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
        log.exception("ingest finalize: merge_paper failed for doc %s — continuing", document_id)

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
        "ingest finalize: doc %s queryable — embedded %d chunks (%d failed)",
        document_id,
        embedded,
        failed,
    )
    return {"document_id": document_id, "queryable": True, "embedded": embedded}
