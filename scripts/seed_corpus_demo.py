#!/usr/bin/env python3
"""
Seed a few demo documents + chunks into the pgvector corpus so /knowledge/stats
lights up and corpus_search / _search_by_vector can be exercised by hand.

Run AFTER migration 015 is applied, with DATABASE_URL set:

    DATABASE_URL=postgres://labfoundry:...@localhost:5432/labfoundry \\
        python -m scripts.seed_corpus_demo
    python -m scripts.seed_corpus_demo --clear      # remove the demo rows

Embeddings are DETERMINISTIC SYNTHETIC vectors (no Ollama needed) — clearly demo
data (source_kind='demo'). Idempotent: upserts on (source_kind, canonical_key).
The vectors are NOT semantically meaningful; they exist so the index, the trust
floor, and the stats endpoint can be demonstrated end-to-end without a model.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os

import asyncpg
import pgvector.asyncpg

EMBED_DIM = 768
SOURCE_KIND = "demo"


def _synthetic_vec(seed: str) -> list[float]:
    """Deterministic, correctly-dimensioned pseudo-embedding seeded from text."""
    vals: list[float] = []
    i = 0
    base = hashlib.blake2b(seed.encode(), digest_size=32).digest()
    while len(vals) < EMBED_DIM:
        block = hashlib.blake2b(base + i.to_bytes(2, "big"), digest_size=64).digest()
        for byte in block:
            vals.append((byte / 127.5) - 1.0)
            if len(vals) >= EMBED_DIM:
                break
        i += 1
    norm = sum(v * v for v in vals) ** 0.5 or 1.0
    return [v / norm for v in vals]


# (kind, title, trust_tier, status, trust_state, queryable, [chunk texts...])
DEMO = [
    (
        "paper",
        "Attention Is All You Need",
        "peer_reviewed",
        "certified",
        "certified",
        True,
        [
            "The Transformer is based solely on attention mechanisms, dispensing with recurrence.",
            "Multi-head attention lets the model jointly attend to information from different subspaces.",
        ],
    ),
    (
        "web",
        "A practitioner blog on RAG patterns",
        "web_reputable",
        "certified",
        "certified",
        True,
        ["Retrieval-augmented generation grounds an LLM in a queryable corpus of documents."],
    ),
    (
        "dataset",
        "OpenML credit-g",
        "official_repo",
        "certified",
        "certified",
        True,
        ["The German credit dataset classifies applicants as good or bad credit risks."],
    ),
    (
        "web",
        "Unverified forum post",
        "web_unknown",
        "certified",
        "provisional",
        True,
        ["A forum user claims a trick that doubles throughput; the claim is unverified."],
    ),
    (
        "web",
        "Blocked spam page",
        "quarantined",
        "blocked",
        "quarantined",
        False,
        ["This page was BLOCKED by Mimir and must never appear in default results."],
    ),
]


async def _seed(conn: asyncpg.Connection) -> None:
    for kind, title, tier, status, state, queryable, chunk_texts in DEMO:
        canonical = f"{SOURCE_KIND}:{title}"
        doc_id = await conn.fetchval(
            """
            INSERT INTO documents (kind, title, source_kind, canonical_key,
                   status, trust_tier, trust_state, queryable, content_hash)
            VALUES ($1::document_kind, $2, $3, $4,
                    $5::document_status, $6::trust_tier, $7::trust_state, $8, $9)
            ON CONFLICT (source_kind, canonical_key) DO NOTHING
            RETURNING id
            """,
            kind,
            title,
            SOURCE_KIND,
            canonical,
            status,
            tier,
            state,
            queryable,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
        if doc_id is None:  # already seeded
            doc_id = await conn.fetchval(
                "SELECT id FROM documents WHERE source_kind=$1 AND canonical_key=$2",
                SOURCE_KIND,
                canonical,
            )
            print(f"  = exists  [{tier:13}] {title}")
        else:
            print(f"  + doc {doc_id:<4} [{tier:13}] {title}")
        for ordinal, text in enumerate(chunk_texts):
            await conn.execute(
                """
                INSERT INTO chunks (document_id, ordinal, text, embedding,
                       embed_model, token_count, content_hash)
                VALUES ($1, $2, $3, $4, 'synthetic-demo', $5, $6)
                ON CONFLICT (document_id, ordinal, content_hash) DO NOTHING
                """,
                doc_id,
                ordinal,
                text,
                _synthetic_vec(text),
                max(1, len(text) // 4),
                hashlib.sha256(text.encode()).hexdigest(),
            )


async def _clear(conn: asyncpg.Connection) -> None:
    n = await conn.fetchval(
        "WITH d AS (DELETE FROM documents WHERE source_kind=$1 RETURNING 1) SELECT COUNT(*) FROM d",
        SOURCE_KIND,
    )
    print(f"removed {n} demo documents (chunks cascade)")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Seed/clear demo corpus rows")
    ap.add_argument("--clear", action="store_true", help="remove demo rows instead of seeding")
    args = ap.parse_args()

    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    await pgvector.asyncpg.register_vector(conn)
    try:
        if args.clear:
            await _clear(conn)
        else:
            print("Seeding demo corpus (synthetic embeddings — no Ollama needed):")
            await _seed(conn)
            print("done. Try: GET /knowledge/stats")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
