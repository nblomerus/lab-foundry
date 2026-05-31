"""Run Mimir's data collectors once, on demand.

Runs the discovery sweep (scouts -> `source.discovered`) against DATABASE_URL.
If the harness is running with MIMIR_LOOP=on it then stages + trust-gates +
ingests each discovered source; otherwise the events queue until it is.

Usage:
    python -m scripts.sweep_library [topic ...]    # topics override LIBRARY_TOPICS
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from agents.mimir.collectors import run_discovery_sweep
from state.client import PostgresClient


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1
    topics = sys.argv[1:] or None
    pool = await asyncpg.create_pool(dsn)
    try:
        result = await run_discovery_sweep(topics, PostgresClient(pool=pool))
    finally:
        await pool.close()
    print(f"discovery sweep: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
