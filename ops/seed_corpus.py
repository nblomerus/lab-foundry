"""
Bulk-seed the Library from the rag-bench arXiv corpus (~21.8k papers).

rag-bench already downloaded, parsed, AND chunked ~21.8k arXiv papers. We reuse
its work directly: the validated chunk texts (`chunks.json`) joined with paper
metadata (`parsed_papers.json`), poured into the lab corpus as the FOUNDATION
Mimir grows from — no re-fetch, no re-chunk, no LLM:

    metadata index (parsed_papers.json: arxiv_id/title/authors/...)
                 +
    chunks.json grouped by doc_id  ->  per paper:
        upsert documents row  ->  stage chunks  ->  arXiv=preprint (deterministic)
        ->  certify  ->  embed with nomic (batched)  ->  queryable

Why reuse rag-bench's chunks rather than our PaperChunker: that chunker is tuned
for markdown/ar5iv section structure and yields nothing on the plain extracted
text in this dump. rag-bench's chunks are already section-aware and powered its
own RAG benchmark, so they're the right granularity. We only re-EMBED (rag-bench
used BAAI/bge at dim 1024 — incompatible with our vector(768)/nomic corpus).

Robustness: chunk texts are sanitized of NUL bytes (Postgres TEXT rejects 0x00;
~14% of this dump carries them). RESUMABLE + idempotent on
(source_kind, canonical_key)=(arxiv, arxiv_id): a paper already queryable is
skipped, one staged-but-unembedded (a prior crash) is finished. Safe to re-run.

Usage:
    python -m ops.seed_corpus --limit 100           # pilot
    python -m ops.seed_corpus                        # full run (resumes)
Env (auto-loaded from .env): DATABASE_URL, OLLAMA_URL, EMBED_MODEL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ops.mimir_firstlight import _load_dotenv, _register_vector_codec

DATA_DIR = "/mnt/data/rag-bench-data"
_OK, _NO, _DOT = "✓", "✗", "•"


def _clean(text: str) -> str:
    """Strip NUL bytes — Postgres TEXT can't store 0x00 (it errors the whole
    INSERT). ~14% of this dump's chunks carry them from PDF extraction."""
    return (text or "").replace("\x00", "")


def _arxiv_id_from_doc(doc_id: str, meta: dict | None) -> str:
    """Prefer the metadata arxiv_id; fall back to stripping the 'arxiv_' prefix
    off the doc_id (chunks.json keys look like 'arxiv_2401.14196')."""
    if meta and (meta.get("arxiv_id") or "").strip():
        return meta["arxiv_id"].strip()
    return doc_id[len("arxiv_") :] if doc_id.startswith("arxiv_") else doc_id


# -------------------------------------------------------------------------
# Batched embedding — the throughput lever (Ollama /api/embed takes an array).
# -------------------------------------------------------------------------


class _BatchEmbedder:
    def __init__(self, ollama_url: str, model: str, dim: int = 768):
        import httpx

        self.url, self.model, self.dim = ollama_url, model, dim
        self._http = httpx.AsyncClient(timeout=180.0)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._http.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts})
        resp.raise_for_status()
        vecs = resp.json().get("embeddings")
        if not vecs or len(vecs) != len(texts):
            raise ValueError(f"embed batch returned {len(vecs or [])} vectors for {len(texts)} inputs")
        if len(vecs[0]) != self.dim:
            raise ValueError(f"embed dim {len(vecs[0])} != {self.dim} (wrong model {self.model!r}?)")
        return vecs

    async def close(self):
        await self._http.aclose()


# -------------------------------------------------------------------------
# Metadata index: doc_id -> {arxiv_id, title, authors, ...}. Built once from
# parsed_papers.json (a 3GB streaming pass) and cached to a small JSON so
# resumed/repeat runs are instant. full_text is dropped (it's the huge part).
# -------------------------------------------------------------------------


def _build_or_load_meta(parsed_path: Path, cache_path: Path) -> dict[str, dict]:
    import ijson

    if cache_path.exists():
        print(f"  {_DOT} loading cached metadata index ({cache_path.name})")
        with cache_path.open() as f:
            return json.load(f)

    print(f"  {_DOT} building metadata index from {parsed_path.name} (one-time 3GB pass)…")
    index: dict[str, dict] = {}
    with parsed_path.open("rb") as fh:
        for p in ijson.items(fh, "item"):
            doc_id = p.get("doc_id")
            if not doc_id:
                continue
            index[doc_id] = {
                "arxiv_id": p.get("arxiv_id"),
                "title": p.get("title"),
                "authors": p.get("authors") or [],
                "categories": p.get("categories") or [],
                "pdf_url": p.get("pdf_url"),
                "year": p.get("year"),
            }
    try:
        with cache_path.open("w") as f:
            json.dump(index, f)
    except Exception:  # noqa: BLE001 — cache is an optimization, not required
        pass
    print(f"  {_DOT} indexed {len(index)} papers")
    return index


def _grouped_chunks(chunks_path: Path):
    """Stream chunks.json, yielding (doc_id, [chunk_text, …]) per paper. Assumes
    chunks are contiguous by doc_id (they are: chunk_id is 'arxiv_<id>_<sec>_<n>',
    written per paper)."""
    import ijson

    cur, texts = None, []
    with chunks_path.open("rb") as fh:
        for ch in ijson.items(fh, "item"):
            d = ch.get("doc_id")
            if d != cur:
                if cur is not None:
                    yield cur, texts
                cur, texts = d, []
            texts.append(ch.get("text") or "")
    if cur is not None:
        yield cur, texts


# -------------------------------------------------------------------------
# Per-paper ingest
# -------------------------------------------------------------------------


async def _ingest(state, embedder, doc_id: str, raw_texts: list[str], meta: dict | None, batch_size: int) -> str:
    """Returns 'new' | 'resume' | 'skip' | 'empty'."""
    texts = [t for t in (_clean(t) for t in raw_texts) if t.strip()]
    if not texts:
        return "empty"
    arxiv_id = _arxiv_id_from_doc(doc_id, meta)
    meta = meta or {}
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"

    doc_db_id, is_new = await state.upsert_document(
        kind="paper",
        source_kind="arxiv",
        canonical_key=arxiv_id,
        title=meta.get("title"),
        authors=meta.get("authors") or [],
        source_url=abs_url,
        arxiv_id=arxiv_id,
        raw_uri=meta.get("pdf_url") or abs_url,
        content_hash=hashlib.sha256("".join(texts).encode("utf-8")).hexdigest(),
    )

    if not is_new:
        doc = await state.get_document(doc_db_id)
        if doc and doc.get("queryable"):
            return "skip"
        outcome = "resume"
    else:
        plan = [
            {
                "ordinal": i,
                "text": t,
                "content_hash": hashlib.sha256(t.encode("utf-8")).hexdigest(),
                "token_count": len(t) // 4,
            }
            for i, t in enumerate(texts)
        ]
        await state.stage_chunk_plan(doc_db_id, plan)
        await state.set_document_trust(doc_db_id, tier="preprint", trust_state="provisional", status="certified")
        await state.append_certification(
            doc_db_id,
            decision="approve",
            to_tier="preprint",
            to_state="provisional",
            signals={"host": "arxiv.org", "arxiv_id": arxiv_id, "source": "rag-bench-bulk"},
            used_llm=False,
            reasons="arXiv preprint (bulk seed from the rag-bench corpus)",
        )
        outcome = "new"

    pending = [c for c in await state.get_chunk_plan(doc_db_id) if not c.get("has_embedding")]
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vecs = await embedder.embed_many([c["text"] for c in batch])
        await state.set_chunk_embeddings(
            doc_db_id,
            [
                {
                    "ordinal": c["ordinal"],
                    "content_hash": c["content_hash"],
                    "embedding": v,
                    "embed_model": embedder.model,
                }
                for c, v in zip(batch, vecs, strict=True)
            ],
        )
    await state.set_document_queryable(doc_db_id, True)
    return outcome


async def run(args: argparse.Namespace) -> int:
    _load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set (and no .env) — cannot run.", file=sys.stderr)
        return 2
    data = Path(args.data_dir)
    parsed_path, chunks_path = data / "parsed_papers.json", data / "chunks.json"
    for p in (parsed_path, chunks_path):
        if not p.exists():
            print(f"{_NO} missing {p}", file=sys.stderr)
            return 2

    import asyncpg

    from library.corpus.tools import EMBED_MODEL
    from state.client import PostgresClient

    print(f"Bulk-seeding from {data} (embedder={EMBED_MODEL}, batch={args.batch_size}, limit={args.limit or 'all'})")
    meta_index = _build_or_load_meta(parsed_path, data / ".lf_meta_index.json")

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=6, init=_register_vector_codec)
    state = PostgresClient(pool=pool)
    embedder = _BatchEmbedder(os.environ.get("OLLAMA_URL", "http://localhost:11434"), EMBED_MODEL)

    counts = {"new": 0, "resume": 0, "skip": 0, "empty": 0, "error": 0}
    chunk_total = 0
    t0 = time.monotonic()
    seen = 0
    try:
        for doc_id, texts in _grouped_chunks(chunks_path):
            if args.limit and seen >= args.limit:
                break
            seen += 1
            try:
                outcome = await _ingest(state, embedder, doc_id, texts, meta_index.get(doc_id), args.batch_size)
                if outcome in {"new", "resume"}:
                    chunk_total += len([t for t in texts if t.strip()])
            except Exception as e:  # noqa: BLE001 — one bad paper must not sink the run
                counts["error"] += 1
                print(f"  {_NO} {doc_id}: {str(e)[:160]}", file=sys.stderr)
                continue
            counts[outcome] += 1
            if seen % args.progress_every == 0:
                dt = max(time.monotonic() - t0, 1e-6)
                print(
                    f"  {_DOT} {seen} papers | new {counts['new']} resume {counts['resume']} "
                    f"skip {counts['skip']} empty {counts['empty']} err {counts['error']} | "
                    f"{seen / dt:.1f} papers/s, {chunk_total / dt:.0f} chunks/s"
                )
    finally:
        await embedder.close()
        n_docs = await pool.fetchval("SELECT count(*) FROM documents WHERE queryable")
        n_chunks = await pool.fetchval("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
        await pool.close()

    dt = time.monotonic() - t0
    print(
        f"\n{_OK} Done: {seen} papers in {dt / 60:.1f} min "
        f"(new {counts['new']}, resumed {counts['resume']}, skipped {counts['skip']}, "
        f"empty {counts['empty']}, errors {counts['error']})"
    )
    print(f"  corpus now: {n_docs} queryable documents, {n_chunks} embedded chunks")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ops.seed_corpus", description="Bulk-seed the Library from rag-bench's arXiv corpus."
    )
    p.add_argument("--data-dir", default=DATA_DIR, help=f"rag-bench data dir (default {DATA_DIR})")
    p.add_argument("--limit", type=int, default=0, help="max papers (0 = all; use for a pilot)")
    p.add_argument("--batch-size", type=int, default=64, help="chunks per embed request (default 64)")
    p.add_argument("--progress-every", type=int, default=100, help="log progress every N papers")
    return p.parse_args(argv)


def main() -> int:
    try:
        return asyncio.run(run(_parse_args()))
    except KeyboardInterrupt:
        print("\ninterrupted — safe to re-run; it resumes where it stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
