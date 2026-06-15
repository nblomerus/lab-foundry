"""Reap orphan research tasks — a one-off cleanup for the migration-026 leak.

A stock-take found department='research' tasks sitting on MISSION/FINDING claims (e.g. the claim #65
finding-claim) — never a real research target. Migration 026 now blocks new ones at INSERT; this halts
the historical zombies. Idempotent: re-running halts nothing once the lab is clean.

    set -a; . ./.env; set +a
    python -m ops.reap_orphan_tasks            # report only (count + claim breakdown)
    python -m ops.reap_orphan_tasks --halt     # actually HALT them
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from state.client import PostgresClient

_PEEK = """
SELECT c.claim_kind, count(*) AS n
FROM tasks t JOIN claims c ON c.id = t.claim_id
WHERE t.department = 'research' AND t.status IN ('pending', 'running')
  AND c.claim_kind IN ('mission', 'finding')
GROUP BY c.claim_kind ORDER BY c.claim_kind
"""


async def run(halt: bool) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = await pool.fetch(_PEEK)
        pending = sum(r["n"] for r in rows)
        if not pending:
            print("✓ no orphan research tasks on mission/finding claims — nothing to reap")
            return 0
        for r in rows:
            print(f"  {r['claim_kind']}: {r['n']} open research task(s)")
        if not halt:
            print(f"\n{pending} orphan task(s) found. Re-run with --halt to reap them.")
            return 0
        n = await PostgresClient(pool=pool).reap_orphan_research_tasks()
        print(f"\n✓ halted {n} orphan research task(s)")
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.reap_orphan_tasks")
    ap.add_argument("--halt", action="store_true", help="actually halt the orphan tasks (default: report only)")
    return asyncio.run(run(ap.parse_args().halt))


if __name__ == "__main__":
    sys.exit(main())
