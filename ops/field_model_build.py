"""
ops.field_model_build — (re)compute Ariadne's field model from the context graph.

The Domain-Expert landscape: every prominent METHOD/TASK/DATASET concept classified by
prominence × share-velocity into emerging | hot | stable | saturated | declining, written
to the `field_model` table (read by Ariadne at deliberation time via read_field_brief).
Re-derivable and cheap; run it after a graph backfill or on a cadence as Mimir ingests.

    set -a; . ./.env; set +a
    python -m ops.field_model_build           # rebuild + summary
    python -m ops.field_model_build --brief    # also print the brief Ariadne will read

Read-only against the graph; replaces the field_model table wholesale.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from library.graph.field_model import build_field_model, read_field_brief
from library.graph.tools import _get_driver


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    driver = await _get_driver()
    try:
        print("building field model from the context graph…")
        s = await build_field_model(driver, pool)
        print(
            f"\n✓ {s['concepts']} concepts classified  "
            f"(cohorts {s['prior']}→{s['recent']}: {s['n_prior']}→{s['n_recent']} papers; "
            f"saturated ≥ {s['sat_threshold']}p)"
        )
        order = ["hot", "emerging", "stable", "saturated", "declining"]
        for st in order:
            print(f"    {st:10} {s['by_state'].get(st, 0)}")

        async with pool.acquire() as conn:
            for st in ("hot", "emerging", "saturated", "declining"):
                rows = await conn.fetch(
                    "SELECT concept_kind, concept_name, total_papers, recent_papers, prior_papers, velocity "
                    "FROM field_model WHERE trend_state = $1 "
                    "ORDER BY CASE WHEN $1 = 'emerging' THEN recent_papers ELSE total_papers END DESC LIMIT 8",
                    st,
                )
                print(f"\n  {st.upper()}:")
                for r in rows:
                    print(
                        f"    {r['concept_kind']:7} {r['concept_name'][:38]:38} "
                        f"total={r['total_papers']:4} {r['prior_papers']}→{r['recent_papers']} "
                        f"vel={float(r['velocity']):+.2f}"
                    )

        if args.brief:
            print("\n" + "=" * 78 + "\nBRIEF (what Ariadne reads):\n" + "=" * 78)
            print(await read_field_brief(pool))
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.field_model_build")
    ap.add_argument("--brief", action="store_true", help="also print the grounding brief Ariadne reads")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
