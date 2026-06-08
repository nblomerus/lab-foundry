"""
Reasoning-layer vertical slice + eval — turn the flat Paper→Source graph into a
queryable Field Model on a small sample, and measure it.

The context graph is Ariadne's "why it matters" backbone, but today it's a flat
provenance stub (Paper-[:FROM]->Source only) — she can't traverse paper→method→paper.
This runner samples N papers, extracts each paper's methods/datasets/tasks with ONE
LLM call (library.graph.extract), projects them into Neo4j, then measures the before/
after and demonstrates the traversals the Field Model needs. It is a SLICE proof: it
establishes the pipeline works and gives a baseline before scaling to the full corpus.

    set -a; . ./.env; set +a
    python -m eval.graph.extract_slice --n 12

Reads the live corpus (Postgres) + writes name-keyed concept nodes/edges to Neo4j —
an eval/build DRIVER (like ops/mimir_firstlight), not a pytest. Idempotent per paper.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import asyncpg

from library.graph.extract import ensure_concept_constraints, extract_paper_concepts, project_paper_concepts
from library.graph.tools import _get_driver

log = logging.getLogger("eval.graph")

_COUNTS_CYPHER = """
RETURN
  COUNT { (n:METHOD) }  AS methods,
  COUNT { (n:DATASET) } AS datasets,
  COUNT { (n:TASK) }    AS tasks,
  COUNT { ()-[r:USES]->() }         AS uses,
  COUNT { ()-[r:EVALUATED_ON]->() } AS eval_on,
  COUNT { ()-[r:ADDRESSES]->() }    AS addresses,
  COUNT { MATCH (p:Paper) WHERE (p)-[:USES|EVALUATED_ON|ADDRESSES]->() } AS papers_with_concepts
"""


async def _measure(driver) -> dict:
    async with driver.session() as session:
        rec = await (await session.run(_COUNTS_CYPHER)).single()
        return dict(rec) if rec else {}


async def _sample(conn, n: int, seed: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT d.id, d.title,
               (SELECT string_agg(c.text, ' ')
                FROM (SELECT text FROM chunks WHERE document_id = d.id
                      ORDER BY ordinal LIMIT 2) c) AS body
        FROM documents d
        WHERE d.kind = 'paper' AND d.status = 'certified' AND d.queryable
          AND d.title IS NOT NULL AND length(d.title) >= 12
        ORDER BY md5(d.id::text || $1::text)
        LIMIT $2
        """,
        str(seed),
        n,
    )
    return [dict(r) for r in rows]


async def _traversals(driver) -> None:
    async with driver.session() as session:
        print("\n  Traversal — methods shared across the sampled papers (paper→method→paper):")
        res = await session.run(
            """
            MATCH (p1:Paper)-[:USES]->(m:METHOD)<-[:USES]-(p2:Paper)
            WHERE p1.id < p2.id
            RETURN m.name AS method, count(DISTINCT p1)+count(DISTINCT p2) AS papers
            ORDER BY papers DESC LIMIT 5
            """
        )
        rows = [r async for r in res]
        if rows:
            for r in rows:
                print(f"    - {r['method']!r}: shared by ~{r['papers']} papers")
        else:
            print("    (no shared methods yet — expected on a tiny sample)")

        print("  Profile — one paper's extracted Field-Model concepts:")
        res = await session.run(
            """
            MATCH (p:Paper)-[r:USES|EVALUATED_ON|ADDRESSES]->(c)
            WITH p, collect(type(r)+':'+c.name) AS concepts
            RETURN p.id AS id, concepts ORDER BY size(concepts) DESC LIMIT 1
            """
        )
        rec = await res.single()
        if rec:
            print(f"    paper {rec['id']}: {rec['concepts'][:12]}")


async def run(n: int, seed: int, model: str | None) -> None:
    dsn = os.environ["DATABASE_URL"]
    driver = await _get_driver()
    await ensure_concept_constraints()

    before = await _measure(driver)
    print(f"BEFORE: {before}")

    conn = await asyncpg.connect(dsn)
    try:
        papers = await _sample(conn, n, seed)
    finally:
        await conn.close()
    log.info("extracting concepts for %d papers (model=%s)...", len(papers), model or "default")

    totals = {"methods": 0, "datasets": 0, "tasks": 0}
    for i, p in enumerate(papers, 1):
        concepts = await extract_paper_concepts(p["title"], p.get("body") or "", **({"model": model} if model else {}))
        w = await project_paper_concepts(p["id"], concepts)
        for k in totals:
            totals[k] += w[k]
        print(
            f"  [{i}/{len(papers)}] paper {p['id']}: "
            f"+{w['methods']}m +{w['datasets']}d +{w['tasks']}t  | {(p['title'] or '')[:54]!r}"
        )

    after = await _measure(driver)
    print(f"\nAFTER:  {after}")
    print(f"projected this run: {totals}")
    cov = after.get("papers_with_concepts", 0)
    print(f"coverage: {cov}/{len(papers)} sampled papers now have ≥1 concept ({cov / max(len(papers), 1):.0%})")
    await _traversals(driver)
    print()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="eval.graph.extract_slice")
    ap.add_argument("--n", type=int, default=12, help="papers to extract")
    ap.add_argument("--seed", type=int, default=7, help="deterministic sample seed")
    ap.add_argument("--model", default=None, help="override GRAPH_EXTRACT_MODEL")
    args = ap.parse_args()
    asyncio.run(run(args.n, args.seed, args.model))


if __name__ == "__main__":
    main()
