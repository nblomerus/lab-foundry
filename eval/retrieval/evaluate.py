"""
Retrieval evaluation harness for the LabFoundry corpus (Ariadne readiness — the
substrate must be *provably* good before the PI reasons over it).

WHY THIS EXISTS
---------------
`library.corpus.corpus_search` is the path Ariadne reads the world through, and it
had ZERO evaluation: no recall@k, no nDCG, no gold set. You cannot claim retrieval
is "perfect" without a number. This harness produces those numbers and, crucially,
exposes *where* the current dense-only path (pgvector ANN + linear rerank) fails —
which is the empirical basis for the "port the full rag-bench hybrid retrieval"
decision (BM25 + dense + cross-encoder + graph injection).

METHOD — known-item retrieval (auto-labelled, no hand-annotation)
-----------------------------------------------------------------
We sample real, certified, queryable documents already in the corpus and, for each,
build queries that *should* retrieve that exact document. The relevant doc is known
(it is the source), so labelling is free and objective. Three query types probe
different failure modes:

  * title    — the document's title. Tests basic semantic recall (dense's strength).
  * passage  — a sentence drawn from the document's own text. Tests passage recall.
  * lexical  — a single distinctive term/acronym from the title. This is where BM25
               wins and pure-dense embeddings often miss; a low lexical score is the
               signal that the hybrid (rag-bench) port is worth it.

Known-item is a *conservative lower bound*: a query may legitimately also retrieve
other on-topic docs, but we only credit the exact source. That is fine for a
baseline and for tracking regressions/improvements.

USAGE
-----
    # one-time: freeze a gold set sampled from the live corpus (deterministic per --seed)
    python -m eval.retrieval.evaluate build --n 60 --seed 7

    # run the eval against the frozen gold set and print a report
    python -m eval.retrieval.evaluate run --k 20

Needs DATABASE_URL (live corpus) and OLLAMA_URL (query embeddings) — the script
loads repo-root .env if they are not already exported. This is an EVAL DRIVER that
intentionally hits the live corpus (like ops/mimir_firstlight.py); it is NOT a
pytest unit test (the suite must not touch live :5432).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
from pathlib import Path

import asyncpg

log = logging.getLogger("eval.retrieval")

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDSET_PATH = Path(__file__).resolve().parent / "goldset.jsonl"

K_CUTOFFS = (1, 5, 10, 20)
QUERY_TYPES = ("title", "passage", "lexical")

# Tiny stopword set for distinctive-term extraction (lexical queries).
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "or",
    "to",
    "in",
    "on",
    "with",
    "via",
    "using",
    "based",
    "from",
    "by",
    "is",
    "are",
    "we",
    "our",
    "this",
    "that",
    "towards",
    "toward",
    "into",
    "as",
    "at",
    "be",
    "can",
    "new",
    "deep",
    "learning",
    "model",
    "models",
    "neural",
    "network",
    "networks",
    "approach",
    "method",
    "methods",
    "framework",
    "system",
    "systems",
    "analysis",
    "study",
    "data",
}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Populate DATABASE_URL / OLLAMA_URL from repo-root .env if not already set."""
    if os.environ.get("DATABASE_URL"):
        return
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def _best_sentence(text: str, lo: int = 8, hi: int = 32) -> str | None:
    """Pick the first sentence whose word count is in [lo, hi] from chunk text."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    for sentence in re.split(r"(?<=[.!?])\s+", cleaned):
        words = sentence.split()
        if lo <= len(words) <= hi and not sentence.startswith("[#"):
            return sentence
    return None


def _distinctive_term(title: str) -> str | None:
    """
    Extract one distinctive term from a title for the lexical (BM25-favouring) probe.
    Preference order: hyphenated/CamelCase/acronym tokens, then the longest non-stop
    alphabetic token. Returns None if nothing usable.
    """
    if not title:
        return None
    # 1) hyphenated terms, CamelCase, or ALL-CAPS acronyms (>=3 chars)
    specials = re.findall(r"\b(?:[A-Za-z]+-[A-Za-z][A-Za-z-]+|[A-Z]{3,}|[A-Z][a-z]+[A-Z][A-Za-z]+)\b", title)
    specials = [s for s in specials if s.lower() not in _STOP]
    if specials:
        return max(specials, key=len)
    # 2) longest non-stopword alphabetic token (>=6 chars to stay distinctive)
    toks = [t for t in re.findall(r"[A-Za-z]{6,}", title) if t.lower() not in _STOP]
    if toks:
        return max(toks, key=len)
    return None


def _build_queries(title: str, chunk_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    t = re.sub(r"\s+", " ", (title or "")).strip()
    if len(t) >= 12:
        out["title"] = t
    sent = _best_sentence(chunk_text)
    if sent:
        out["passage"] = sent
    term = _distinctive_term(t)
    if term:
        out["lexical"] = term
    return out


# ---------------------------------------------------------------------------
# Gold set build (samples the live corpus, deterministic per seed)
# ---------------------------------------------------------------------------


async def build_goldset(n: int, seed: int) -> int:
    _load_dotenv()
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        # Deterministic sample: certified, queryable papers with a real title and a
        # content-rich chunk. md5(id || seed) gives a stable pseudo-random order.
        rows = await conn.fetch(
            """
            SELECT d.id, d.title,
                   (SELECT c.text FROM chunks c
                     WHERE c.document_id = d.id AND c.text IS NOT NULL
                     ORDER BY c.token_count DESC NULLS LAST LIMIT 1) AS chunk_text
            FROM documents d
            WHERE d.kind = 'paper'
              AND d.status = 'certified' AND d.queryable
              AND d.trust_state NOT IN ('quarantined','decayed')
              AND d.title IS NOT NULL AND length(d.title) >= 20
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id
                          AND c.embedding IS NOT NULL)
            ORDER BY md5(d.id::text || $1::text)
            LIMIT $2
            """,
            str(seed),
            n * 2,  # oversample; some rows won't yield all query types
        )
    finally:
        await conn.close()

    items: list[dict] = []
    for r in rows:
        queries = _build_queries(r["title"], r["chunk_text"] or "")
        if "title" not in queries:  # require at least the title probe
            continue
        items.append({"document_id": r["id"], "title": r["title"], "queries": queries})
        if len(items) >= n:
            break

    with GOLDSET_PATH.open("w") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    log.info("wrote %d gold-set items -> %s", len(items), GOLDSET_PATH)
    return len(items)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _doc_rank(chunks, target_id: int) -> int | None:
    """1-based rank of the first chunk belonging to target_id (dedupe by document)."""
    seen: set[int] = set()
    rank = 0
    for c in chunks:
        if c.document_id in seen:
            continue
        seen.add(c.document_id)
        rank += 1
        if c.document_id == target_id:
            return rank
    return None


def _ndcg_at(rank: int | None, k: int) -> float:
    """Single-relevant-item nDCG@k: IDCG=1, so nDCG = 1/log2(rank+1) if rank<=k."""
    if rank is None or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def run_eval(k: int, min_trust: str | None, mode: str = "hybrid") -> None:
    _load_dotenv()
    if not GOLDSET_PATH.exists():
        raise SystemExit(f"no gold set at {GOLDSET_PATH} — run `build` first")

    from library.corpus import tools  # local corpus read path (embeds via Ollama)

    items = [json.loads(line) for line in GOLDSET_PATH.read_text().splitlines() if line.strip()]
    hybrid = mode == "hybrid"
    log.info("loaded %d gold-set items; retrieving k=%d mode=%s (min_trust=%s)", len(items), k, mode, min_trust)

    # accumulator: per query-type -> list of (rank or None)
    ranks: dict[str, list[int | None]] = {qt: [] for qt in QUERY_TYPES}
    failures: list[tuple[str, str, str]] = []  # (qtype, query, title)

    for it in items:
        target = it["document_id"]
        for qt, query in it["queries"].items():
            chunks = await tools.corpus_search(query, k=k, min_trust=min_trust, hybrid=hybrid)
            r = _doc_rank(chunks, target)
            ranks.setdefault(qt, []).append(r)
            if r is None:
                failures.append((qt, query[:70], (it["title"] or "")[:70]))

    _report(ranks, k, failures, mode)


def _report(ranks: dict[str, list[int | None]], k: int, failures, mode: str = "hybrid") -> None:
    def line(label: str, rs: list[int | None]) -> str:
        n = len(rs)
        if n == 0:
            return f"  {label:<10} (no queries)"
        found = [r for r in rs if r is not None]
        rec = {c: sum(1 for r in found if r <= c) / n for c in K_CUTOFFS if c <= k}
        mrr = sum(1.0 / r for r in found) / n
        ndcg = sum(_ndcg_at(r, 10) for r in rs) / n
        rec_str = "  ".join(f"R@{c}={rec[c]:.2f}" for c in rec)
        return f"  {label:<10} n={n:<4} {rec_str}  MRR={mrr:.3f}  nDCG@10={ndcg:.3f}"

    label = "hybrid: dense ⊕ BM25 (RRF)" if mode == "hybrid" else "dense-only ANN + linear rerank"
    print("\n" + "=" * 78)
    print(f"RETRIEVAL EVAL — corpus_search [{label}], k={k}")
    print("=" * 78)
    all_ranks: list[int | None] = []
    for qt in QUERY_TYPES:
        if ranks.get(qt):
            print(line(qt, ranks[qt]))
            all_ranks.extend(ranks[qt])
    print("-" * 78)
    print(line("OVERALL", all_ranks))
    print("=" * 78)

    if failures:
        # Surface where it breaks — the actionable part for the rag-bench decision.
        by_type: dict[str, int] = {}
        for qt, _, _ in failures:
            by_type[qt] = by_type.get(qt, 0) + 1
        print(
            f"\nNOT-FOUND in top-{k} (target missed entirely): {len(failures)} total — "
            + ", ".join(f"{qt}:{c}" for qt, c in sorted(by_type.items()))
        )
        print("worst-case misses (query → expected doc):")
        for qt, q, title in failures[:15]:
            print(f"  [{qt:<7}] {q!r}\n             → {title!r}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="eval.retrieval.evaluate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="sample the live corpus into a frozen gold set")
    b.add_argument("--n", type=int, default=60, help="number of gold-set documents")
    b.add_argument("--seed", type=int, default=7, help="deterministic sampling seed")

    r = sub.add_parser("run", help="evaluate corpus_search against the frozen gold set")
    r.add_argument("--k", type=int, default=20, help="retrieval depth")
    r.add_argument("--min-trust", default=None, help="optional trust-tier floor")
    r.add_argument(
        "--mode", choices=("hybrid", "dense"), default="hybrid", help="hybrid (dense⊕BM25 RRF) or dense-only (legacy)"
    )

    args = ap.parse_args()
    if args.cmd == "build":
        n = asyncio.run(build_goldset(args.n, args.seed))
        print(f"gold set: {n} documents -> {GOLDSET_PATH}")
    elif args.cmd == "run":
        asyncio.run(run_eval(args.k, args.min_trust, args.mode))


if __name__ == "__main__":
    main()
