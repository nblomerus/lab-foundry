"""
Experiment auditor — one read-only sweep of the experiment lane's health, by researcher.

The lab's experiments were failing ~38% with the cause hidden behind a generic "no JSON result". The
session loop now records a `failure_class` (migration 023) and surfaces the most informative error, and
every run is owned by a researcher (migration 022). This tool makes the picture legible on demand:

    set -a; . ./.env; set +a
    python -m ops.experiment_audit [--days 14] [--recent 15]
    python -m ops.experiment_audit --backfill-realism      # (write) classify legacy NULL-realism runs

Shows: status distribution · failure_class buckets · data-realism distribution · per-researcher
track record · open capability-gap signals (needs_capability) · the most recent failures with their
class + headline error. Read-only by default; --backfill-realism is the only writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import asyncpg
from dotenv import load_dotenv

from agents.experiments.handler import _classify_realism


def _h(title: str) -> None:
    print(f"\n{title}")


async def _overview(conn, days: int) -> None:
    _h(f"Status distribution (last {days}d)")
    for r in await conn.fetch(
        "SELECT status, count(*) n FROM experiment_runs "
        "WHERE started_at > now() - make_interval(days => $1) OR started_at IS NULL "
        "GROUP BY status ORDER BY n DESC",
        days,
    ):
        print(f"  {r['status']:12} {r['n']}")

    _h("Failure class (failed/killed runs)")
    rows = await conn.fetch(
        "SELECT coalesce(failure_class, '(unclassified)') fc, count(*) n FROM experiment_runs "
        "WHERE status IN ('failed','killed') GROUP BY fc ORDER BY n DESC"
    )
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r['fc']:18} {r['n']}")

    _h("Data realism")
    for r in await conn.fetch(
        "SELECT coalesce(data_realism, '(unset)') dr, count(*) n FROM experiment_runs GROUP BY dr ORDER BY n DESC"
    ):
        print(f"  {r['dr']:18} {r['n']}")


async def _by_researcher(conn) -> None:
    _h("Per researcher")
    rows = await conn.fetch(
        """
        SELECT coalesce(r.name, '(unassigned)') name,
               count(*) FILTER (WHERE e.status = 'completed') done,
               count(*) FILTER (WHERE e.status IN ('failed','killed')) failed,
               count(*) FILTER (WHERE e.failure_class = 'infeasible') infeasible
        FROM experiment_runs e LEFT JOIN researchers r ON r.id = e.researcher_id
        GROUP BY name ORDER BY done DESC, failed DESC
        """
    )
    print(f"  {'researcher':<16} {'done':>5} {'fail':>5} {'infeas':>7}  win%")
    for r in rows:
        tot = r["done"] + r["failed"]
        rate = f"{100 * r['done'] / tot:.0f}" if tot else "—"
        print(f"  {r['name']:<16} {r['done']:>5} {r['failed']:>5} {r['infeasible']:>7}  {rate:>4}")


async def _capability_gaps(conn, days: int) -> None:
    _h(f"Open capability gaps (needs_capability signals, last {days}d)")
    rows = await conn.fetch(
        "SELECT payload->>'capability' cap, count(*) n, max(emitted_at) last FROM events "
        "WHERE event_type = 'loop.unclosed' AND payload->>'kind' = 'needs_capability' "
        "AND emitted_at > now() - make_interval(days => $1) GROUP BY cap ORDER BY n DESC",
        days,
    )
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {str(r['cap']):16} {r['n']:>3}   last={r['last']:%Y-%m-%d}")


async def _recent_failures(conn, n: int) -> None:
    _h(f"Most recent {n} failures")
    rows = await conn.fetch(
        "SELECT e.id, coalesce(r.name,'?') who, coalesce(e.failure_class,'?') fc, left(coalesce(e.error,''), 90) err "
        "FROM experiment_runs e LEFT JOIN researchers r ON r.id = e.researcher_id "
        "WHERE e.status IN ('failed','killed') ORDER BY e.id DESC LIMIT $1",
        n,
    )
    for r in rows:
        print(f"  #{r['id']:<4} {r['who']:<10} {r['fc']:<16} {r['err']}")


async def _backfill_realism(conn) -> None:
    """One-off: classify completed runs whose data_realism is NULL (legacy rows) from their code +
    self-reported dataset source. Write-only path."""
    rows = await conn.fetch(
        "SELECT id, code, result FROM experiment_runs WHERE status = 'completed' AND data_realism IS NULL"
    )
    n = 0
    for r in rows:
        result = r["result"]
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {}
        ds = (result or {}).get("dataset") or (result or {}).get("datasets") or {}
        ds_source = ds.get("source", "") if isinstance(ds, dict) else ""
        realism = _classify_realism(r["code"] or "", ds_source)
        await conn.execute("UPDATE experiment_runs SET data_realism = $1 WHERE id = $2", realism, r["id"])
        n += 1
    print(f"backfilled data_realism on {n} completed run(s)")


async def _main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Experiment lane auditor")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--recent", type=int, default=15)
    ap.add_argument("--backfill-realism", action="store_true", help="(write) classify legacy NULL-realism runs")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            if args.backfill_realism:
                await _backfill_realism(conn)
                return
            await _overview(conn, args.days)
            await _by_researcher(conn)
            await _capability_gaps(conn, args.days)
            await _recent_failures(conn, args.recent)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
