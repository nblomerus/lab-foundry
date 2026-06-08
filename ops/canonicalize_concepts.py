"""
Canonicalize the context-graph concept nodes — merge surface variants of the same
concept (LLM/LLMs/large language model(s), fine-tuning/finetuning, diffusion model/
models, RAG/retrieval-augmented generation, …) into one node, folding their paper
edges together.

Without this, a concept's true paper-count (its saturation signal) is split across
variants and traversals miss connections (papers using "LLMs" don't link to "LLM").
Uses the same `_canon_key` the extractor now writes (library.graph.extract), so this
is a one-time backfill; future extractions are already canonical.

Plain Cypher (no APOC): per label, ensure the canonical node, rewire all Paper edges
from each variant to it, then delete the now-edgeless variants. Idempotent.

    set -a; . ./.env; set +a
    python -m ops.canonicalize_concepts            # apply
    python -m ops.canonicalize_concepts --dry-run  # report what would merge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from dotenv import load_dotenv

from library.graph.extract import _canon_key
from library.graph.tools import _get_driver

# (node label, edge type from Paper)
_LABELS = [("METHOD", "USES"), ("DATASET", "EVALUATED_ON"), ("TASK", "ADDRESSES")]


async def _plan(session, label: str, rel: str):
    """Group existing nodes by canonical key; return (canon[], pairs[], dead[], stats)."""
    res = await session.run(
        f"MATCH (n:{label}) "
        f"OPTIONAL MATCH (n)<-[:{rel}]-(p:Paper) "
        f"RETURN n.key AS key, n.name AS name, count(p) AS papers"
    )
    groups: dict[str, list] = defaultdict(list)
    total = 0
    async for r in res:
        total += 1
        groups[_canon_key(r["name"] or r["key"] or "")].append((r["key"], r["name"], r["papers"]))

    canon, pairs, dead = [], [], []
    merged_groups = 0
    for ck, members in groups.items():
        keys = {m[0] for m in members}
        # work needed if more than one node, or the lone node isn't already canonical
        if len(keys) == 1 and ck in keys:
            continue
        merged_groups += 1
        disp = max(members, key=lambda m: m[2])[1] or ck  # display = most-cited variant
        canon.append({"ck": ck, "disp": disp})
        for vk in keys:
            if vk != ck:
                pairs.append({"vk": vk, "ck": ck})
                dead.append(vk)
    return canon, pairs, dead, {"nodes": total, "groups_after": len(groups), "merged_groups": merged_groups}


async def run(dry_run: bool) -> int:
    load_dotenv()
    driver = await _get_driver()
    for label, rel in _LABELS:
        async with driver.session() as session:
            canon, pairs, dead, stats = await _plan(session, label, rel)
            print(
                f"{label}: {stats['nodes']} nodes -> {stats['groups_after']} canonical "
                f"({stats['merged_groups']} groups need merging, {len(dead)} variant nodes folded)"
            )
            if dry_run or not pairs:
                continue
            # 1. ensure canonical nodes exist with the chosen display name (cheap MERGE on indexed key)
            await session.run(
                f"UNWIND $canon AS c MERGE (n:{label} {{key: c.ck}}) SET n.name = c.disp, n.concepts_extracted = true",
                canon=canon,
            )
            # 2+3. rewire edges then delete variants, CHUNKED (avoids one huge transaction)
            chunk = 1000
            for i in range(0, len(pairs), chunk):
                batch = pairs[i : i + chunk]
                await session.run(
                    f"UNWIND $pairs AS pr "
                    f"MATCH (p:Paper)-[r:{rel}]->(v:{label} {{key: pr.vk}}) "
                    f"MATCH (c:{label} {{key: pr.ck}}) "
                    f"MERGE (p)-[:{rel}]->(c) DELETE r",
                    pairs=batch,
                )
                await session.run(
                    f"UNWIND $dead AS k MATCH (v:{label} {{key: k}}) DETACH DELETE v",
                    dead=[pr["vk"] for pr in batch],
                )
            print(f"  {label}: folded {len(dead)} variants ✓")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.canonicalize_concepts")
    ap.add_argument("--dry-run", action="store_true", help="report merges without applying")
    return asyncio.run(run(ap.parse_args().dry_run))


if __name__ == "__main__":
    sys.exit(main())
