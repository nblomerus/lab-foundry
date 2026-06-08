"""
The mode dial — inspect / set per-agent modes (off|shadow|advisory|active).

    set -a; . ./.env; set +a
    python -m ops.agent_mode list
    python -m ops.agent_mode set mimir off  --note "paused to debug ingest"
    python -m ops.agent_mode set ariadne shadow

off|shadow pause the agent at the dispatcher; advisory|active run it. Takes effect
within ~5s (the dispatcher's mode cache TTL). See harness/agent_modes.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from harness.agent_modes import _RESEARCH, _default_mode, set_agent_mode


async def _list(pool) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT agent_name, mode, note FROM agent_modes ORDER BY agent_name")
    explicit = {r["agent_name"] for r in rows}
    print("explicit modes (override defaults):")
    for r in rows:
        print(f"  {r['agent_name']:<14} {r['mode']:<9} {r['note'] or ''}")
    if not rows:
        print("  (none set)")
    print("defaults (no row — derived from KNOWLEDGE_CORE_ONLY):")
    for a in sorted({"mimir"} | _RESEARCH):
        if a not in explicit:
            print(f"  {a:<14} {_default_mode(a):<9} (default)")


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        if args.cmd == "list":
            await _list(pool)
        else:
            await set_agent_mode(pool, args.agent, args.mode, args.note)
            print(f"set {args.agent} -> {args.mode}" + (f"  ({args.note})" if args.note else ""))
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.agent_mode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show explicit modes + defaults")
    s = sub.add_parser("set", help="set an agent's mode")
    s.add_argument("agent")
    s.add_argument("mode", choices=["off", "shadow", "advisory", "active"])
    s.add_argument("--note", default=None)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
