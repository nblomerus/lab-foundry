"""
ops.decay_corpus — demote stale, low-trust, UNUSED documents to trust_state='decayed'.

The corpus carries a large low-trust mass (≈40% github at web_unknown, much of it noise). The
retrieval gate already EXCLUDES trust_state='decayed' (library/corpus/tools.py), but nothing ever
set it — 'decayed' was a designed-but-unused state. This sweep demotes a doc to 'decayed' (removed
from search, NOT deleted — reversible by re-ingest) when ALL of these hold:

  - trust_state='provisional' AND status='certified'        (a live corpus doc)
  - trust_tier in the decay set (default: web_unknown — the lowest non-quarantined tier)
  - source_url is not 'lab://…'                             (never decay the lab's own output)
  - ingested_at older than --age-days
  - last_retrieved_at older than --unused-days, or NULL     (it isn't actually being used)

Defaults are conservative and --dry-run is the DEFAULT (reports only; --apply to write). NOTE:
last_retrieved_at is stamped by library.corpus.tools._track_retrieval going forward, so on the FIRST
run after migration 021 most docs have it NULL (look "never retrieved") — review the dry-run count
before --apply. Let tracking run for a while first if you want "unused" to mean something.

    set -a; . ./.env; set +a
    python -m ops.decay_corpus                                              # dry-run (report only)
    python -m ops.decay_corpus --apply                                     # demote
    python -m ops.decay_corpus --tiers web_unknown,web --age-days 120 --unused-days 60 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

_WHERE = """
    status = 'certified' AND trust_state = 'provisional'
    AND trust_tier = ANY($1::trust_tier[])
    AND coalesce(source_url, '') NOT LIKE 'lab://%'
    AND ingested_at < now() - make_interval(days => $2)
    AND (last_retrieved_at IS NULL OR last_retrieved_at < now() - make_interval(days => $3))
"""


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            params = (tiers, args.age_days, args.unused_days)
            total = await conn.fetchval(f"SELECT count(*) FROM documents WHERE {_WHERE}", *params)
            by_kind = await conn.fetch(
                f"SELECT source_kind, count(*) AS n FROM documents WHERE {_WHERE} GROUP BY source_kind ORDER BY n DESC",
                *params,
            )
            print(f"decay candidates (tiers={tiers}, age>{args.age_days}d, unused>{args.unused_days}d): {total}")
            for r in by_kind:
                print(f"    {r['source_kind']:10} {r['n']}")
            if not args.apply:
                print("\n(dry-run — re-run with --apply to demote to trust_state='decayed'; reversible by re-ingest)")
                return 0
            tag = await conn.execute(f"UPDATE documents SET trust_state = 'decayed' WHERE {_WHERE}", *params)
            print(f"\n✓ demoted {tag.split()[-1]} documents to 'decayed' (now excluded from corpus_search)")
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.decay_corpus")
    ap.add_argument("--tiers", default="web_unknown", help="comma-separated trust tiers to decay (default: web_unknown)")
    ap.add_argument("--age-days", type=int, default=180, help="only decay docs ingested more than N days ago")
    ap.add_argument("--unused-days", type=int, default=90, help="only decay docs not retrieved in N days (NULL = unused)")
    ap.add_argument("--apply", action="store_true", help="actually demote (default is dry-run)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
