"""
ops.backfill_trace_graph — project EXISTING research-era findings into the trace graph.

The live loop now projects each finding into the graph as it lands (grounded researcher →
GROUNDS+CITES, synthesis → GROUNDS), but that only covers findings produced AFTER the change.
This one-off backfills the rows already in the DB so /trace isn't empty until new research runs:

  - research_findings  → (:Finding {FINDING_ID_SYNTHESIS + id})-[:GROUNDS]->(:Claim)
  - completed research tasks with evidence
                       → (:Finding {FINDING_ID_RESEARCHER + task_id})-[:GROUNDS]->(:Claim)

LOSSY by design: the researcher's CITES edges need the resolved paper document_ids, which were
never persisted in the task result — so the backfill recovers GROUNDS only. New research gets full
GROUNDS+CITES going forward. Idempotent (MERGE on a namespaced finding id).

    set -a; . ./.env; set +a
    python -m ops.backfill_trace_graph --dry-run   # report what WOULD project (no writes)
    python -m ops.backfill_trace_graph             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv

from library.graph.tools import FINDING_ID_RESEARCHER, FINDING_ID_SYNTHESIS, merge_finding_grounds_claim


def _supports(verdict: str | None) -> bool | None:
    return True if verdict in ("yes", "supports") else False if verdict in ("no", "contradicts") else None


async def _backfill_synthesis(conn, *, dry_run: bool) -> int:
    rows = await conn.fetch(
        "SELECT id, direction_claim_id, headline, claim_text, supported, confidence, created_at "
        "FROM research_findings WHERE direction_claim_id IS NOT NULL"
    )
    for r in rows:
        if dry_run:
            continue
        await merge_finding_grounds_claim(
            finding_id=FINDING_ID_SYNTHESIS + r["id"],
            claim_id=r["direction_claim_id"],
            source="synthesis",
            url=None,
            title=(r["headline"] or "")[:200],
            summary=(r["claim_text"] or "")[:1000],
            relevance_score=round(float(r["confidence"] or 0) * 10, 1),
            supports_claim=_supports(r["supported"]),
            audit_verdict=r["supported"],
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
    return len(rows)


async def _backfill_researcher(conn, *, dry_run: bool) -> int:
    rows = await conn.fetch(
        "SELECT id, claim_id, result, completed_at FROM tasks "
        "WHERE department = 'research' AND status = 'completed' AND claim_id IS NOT NULL "
        "AND COALESCE((result->>'n_evidence')::int, 0) > 0"
    )
    n = 0
    for t in rows:
        res = t["result"]
        if isinstance(res, str):
            res = json.loads(res or "{}")
        res = res or {}
        title = (res.get("summary") or "")[:200]
        if dry_run:
            n += 1
            continue
        await merge_finding_grounds_claim(
            finding_id=FINDING_ID_RESEARCHER + t["id"],
            claim_id=t["claim_id"],
            source="researcher",
            url=None,
            title=title,
            summary=title,
            relevance_score=round(float(res.get("confidence") or 0) * 10, 1),
            supports_claim=_supports(res.get("verdict")),
            audit_verdict=res.get("disposition"),
            created_at=t["completed_at"].isoformat() if t["completed_at"] else "",
        )
        n += 1
    return n


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            syn = await _backfill_synthesis(conn, dry_run=args.dry_run)
            res = await _backfill_researcher(conn, dry_run=args.dry_run)
    finally:
        await pool.close()
    verb = "would project" if args.dry_run else "projected"
    prefix = "(dry-run) " if args.dry_run else ""
    print(f"{prefix}✓ {verb}: {syn} synthesis + {res} researcher findings → :Finding-[:GROUNDS]->:Claim")
    if args.dry_run:
        print("  (re-run without --dry-run to apply; CITES edges are NOT backfilled — see module docstring)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.backfill_trace_graph")
    ap.add_argument("--dry-run", action="store_true", help="report counts without writing to the graph")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
