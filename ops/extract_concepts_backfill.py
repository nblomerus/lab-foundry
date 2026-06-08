"""
Backfill the context-graph reasoning layer over the whole corpus.

Extracts each paper's methods/datasets/tasks (one LLM call — library.graph.extract)
and projects them into Neo4j, turning the flat Paper→Source graph into Ariadne's
queryable Field Model. RESUMABLE + idempotent: a `p.concepts_extracted` marker is
set per paper (even when it has 0 concepts), so a re-run skips done papers and a
death/restart costs nothing. The graph grows incrementally — usable at any point.

GPU-bound (~3–5 s/paper; concurrency gives no speedup), so the full ~23k corpus is a
~24–30 h job. It shares the GPU with the discovery pump's embedder, so both run
slower while this is active. Run a bounded slice with --limit, or the whole corpus
with --limit 0.

    set -a; . ./.env; set +a
    python -m ops.extract_concepts_backfill --limit 0           # full corpus, resumable
    python -m ops.extract_concepts_backfill --limit 500         # bounded chunk

Re-running picks up new papers (added by the discovery pump) automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import asyncpg
from dotenv import load_dotenv

from library.graph.extract import (
    ensure_concept_constraints,
    extract_paper_concepts,
    extracted_paper_ids,
    project_paper_concepts,
)


async def _candidate_ids(conn) -> list[int]:
    rows = await conn.fetch(
        """
        SELECT d.id FROM documents d
        WHERE d.kind = 'paper' AND d.status = 'certified' AND d.queryable
          AND d.title IS NOT NULL AND length(d.title) >= 12
          AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)
        ORDER BY d.id
        """
    )
    return [r["id"] for r in rows]


async def _fetch_bodies(conn, ids: list[int]) -> dict[int, dict]:
    rows = await conn.fetch(
        """
        SELECT d.id, d.title,
               (SELECT string_agg(c.text, ' ')
                FROM (SELECT text FROM chunks WHERE document_id = d.id
                      ORDER BY ordinal LIMIT 2) c) AS body
        FROM documents d WHERE d.id = ANY($1::bigint[])
        """,
        ids,
    )
    return {r["id"]: dict(r) for r in rows}


async def run(limit: int, model: str | None, progress_every: int, batch: int) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    await ensure_concept_constraints()
    done = await extracted_paper_ids()

    conn = await asyncpg.connect(dsn)
    try:
        candidates = await _candidate_ids(conn)
        todo = [i for i in candidates if i not in done]
        if limit:
            todo = todo[:limit]
        print(
            f"corpus: {len(candidates)} papers | already extracted: {len(done)} | "
            f"to do this run: {len(todo)} (model={model or 'default'})"
        )

        kit = {"methods": 0, "datasets": 0, "tasks": 0}
        empty = 0
        t0 = time.monotonic()
        for start in range(0, len(todo), batch):
            chunk_ids = todo[start : start + batch]
            bodies = await _fetch_bodies(conn, chunk_ids)
            for pid in chunk_ids:
                p = bodies.get(pid)
                if not p:
                    continue
                concepts = await extract_paper_concepts(
                    p["title"], p.get("body") or "", **({"model": model} if model else {})
                )
                w = await project_paper_concepts(pid, concepts)
                for k in kit:
                    kit[k] += w[k]
                if w["methods"] + w["datasets"] + w["tasks"] == 0:
                    empty += 1
                seen = start + chunk_ids.index(pid) + 1
                if seen % progress_every == 0:
                    dt = max(time.monotonic() - t0, 1e-6)
                    rate = seen / dt
                    eta_h = (len(todo) - seen) / rate / 3600 if rate else 0
                    print(
                        f"  {seen}/{len(todo)} | {rate:.2f} papers/s | "
                        f"+{kit['methods']}m +{kit['datasets']}d +{kit['tasks']}t | "
                        f"{empty} empty | ETA {eta_h:.1f}h"
                    )
    finally:
        await conn.close()

    dt = time.monotonic() - t0
    print(f"\nDone this run: {len(todo)} papers in {dt / 60:.1f} min. Projected: {kit} ({empty} papers had no concepts).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.extract_concepts_backfill")
    ap.add_argument("--limit", type=int, default=0, help="max papers this run (0 = all remaining)")
    ap.add_argument("--model", default=None, help="override GRAPH_EXTRACT_MODEL")
    ap.add_argument("--progress-every", type=int, default=100)
    ap.add_argument("--batch", type=int, default=200, help="body-fetch batch size")
    args = ap.parse_args()
    return asyncio.run(run(args.limit, args.model, args.progress_every, args.batch))


if __name__ == "__main__":
    sys.exit(main())
