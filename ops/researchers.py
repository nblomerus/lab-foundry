"""Researcher roster admin — list / add / set-status / backfill the lab's researchers.

The roster (migration 022) is the set of named, full-stack researchers that own directions and author
experiments. This CLI inspects and edits it.

    set -a; . ./.env; set +a
    python -m ops.researchers                       # list the roster + per-researcher load
    python -m ops.researchers add --name Archimedes --specialty systems-optimization \
        --persona "Loves a clean proof and a faster kernel."
    python -m ops.researchers set-status --name Heron --status paused
    python -m ops.researchers backfill              # assign approved-but-unowned directions

Read-only by default (the bare `list`); the other subcommands write.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from agents.researcher.assign import backfill_unassigned


async def _list(pool) -> None:
    rows = await pool.fetch(
        """
        SELECT r.id, r.name, r.specialty, r.status,
               (SELECT count(*) FROM claims c
                  WHERE c.researcher_id = r.id AND c.claim_kind = 'direction' AND c.status <> 'concluded') AS owned,
               (SELECT count(*) FROM experiment_runs e WHERE e.researcher_id = r.id) AS exps,
               (SELECT count(*) FROM experiment_runs e
                  WHERE e.researcher_id = r.id AND e.status = 'completed') AS done,
               (SELECT count(*) FROM experiment_runs e
                  WHERE e.researcher_id = r.id AND e.status IN ('failed','killed')) AS failed
        FROM researchers r ORDER BY r.id
        """
    )
    if not rows:
        print("(empty roster — run a migration or `add`)")
        return
    hdr = f"{'id':>3}  {'name':<14} {'specialty':<24} {'status':<8} {'owned':>5} {'done':>5} {'fail':>5}  win%"
    print(hdr)
    for r in rows:
        done = r["done"] or 0
        failed = r["failed"] or 0
        rate = f"{100 * done / (done + failed):.0f}" if (done + failed) else "—"
        print(
            f"{r['id']:>3}  {r['name']:<14} {(r['specialty'] or ''):<24} {r['status']:<8} "
            f"{r['owned']:>5} {done:>5} {failed:>5}  {rate:>4}"
        )


async def _add(pool, name: str, specialty: str, persona: str, model: str | None) -> None:
    rid = await pool.fetchval(
        "INSERT INTO researchers (name, specialty, persona, model) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (name) DO UPDATE SET specialty = EXCLUDED.specialty, persona = EXCLUDED.persona, "
        "model = EXCLUDED.model RETURNING id",
        name,
        specialty,
        persona,
        model,
    )
    print(f"upserted researcher #{rid} {name}")


async def _set_status(pool, name: str, status: str) -> None:
    rid = await pool.fetchval("UPDATE researchers SET status = $1 WHERE name = $2 RETURNING id", status, name)
    print(f"{name}: status -> {status}" if rid else f"no researcher named {name}")


async def _backfill(pool) -> None:
    n = await backfill_unassigned(pool)
    print(f"assigned {n} previously-unowned direction(s)")


async def _main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Researcher roster admin")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--specialty", default="")
    a.add_argument("--persona", default="")
    a.add_argument("--model", default=None)
    s = sub.add_parser("set-status")
    s.add_argument("--name", required=True)
    s.add_argument("--status", required=True, choices=["active", "paused", "retired"])
    sub.add_parser("backfill")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        if args.cmd == "add":
            await _add(pool, args.name, args.specialty, args.persona, args.model)
        elif args.cmd == "set-status":
            await _set_status(pool, args.name, args.status)
        elif args.cmd == "backfill":
            await _backfill(pool)
        else:
            await _list(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
