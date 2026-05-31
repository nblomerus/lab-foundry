"""Ask Mimir to acquire a source, on demand (MIMIR_WARDEN_SCOPE §5).

Emits an `acquire.requested` event; a running harness (MIMIR_LOOP=on) adjudicates
and ingests it. Give one identifier (--arxiv-id / --url / --doi) or a --query.

Usage:
    python -m scripts.acquire --why "grounds the speculative-decoding claim" --arxiv-id 2401.00001
    python -m scripts.acquire --requester researcher --why "..." --query "KV cache compression"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from agents.mimir.acquire import AcquireRequest, request_acquire
from state.client import PostgresClient


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--requester", default="pi", help="pi | researcher | novelty")
    p.add_argument("--why", required=True, help="justification (>=30 chars)")
    p.add_argument("--arxiv-id")
    p.add_argument("--url")
    p.add_argument("--doi")
    p.add_argument("--query")
    p.add_argument("--claim-id", type=int)
    a = p.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    req = AcquireRequest(
        requester=a.requester,
        why=a.why,
        arxiv_id=a.arxiv_id,
        url=a.url,
        doi=a.doi,
        query=a.query,
        claim_id=a.claim_id,
    )
    pool = await asyncpg.create_pool(dsn)
    try:
        await request_acquire(PostgresClient(pool=pool), req)
    finally:
        await pool.close()
    print(f"queued acquire.requested ({req.requester}): {req.why[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
