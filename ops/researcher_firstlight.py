"""
Researcher first light — run the Library-grounded Researcher in SHADOW over the pending
research tasks and print the findings. Writes NOTHING (no task claim, no finding row, no
events): a read-only dress rehearsal so you can see what evidence each task would surface
and whether it supports / contradicts / can't-yet-settle Ariadne's expectation.

    python -m ops.researcher_firstlight            # all pending research tasks
    python -m ops.researcher_firstlight --limit 2  # just the first couple
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from agents.researcher.feedback import aggregate_direction, finding_feedback
from agents.researcher.grounded import grade_finding, investigate_task
from state.client import PostgresClient

_VERDICT_MARK = {"supports": "✓ supports", "contradicts": "✗ contradicts", "inconclusive": "? inconclusive"}
_DISP_MARK = {
    "supported": "✓ supported",
    "contradicted": "✗ contradicted",
    "thin_corpus": "▸ thin corpus (acquire)",
    "needs_experiment": "⚗ needs experiment",
    "inconclusive": "? inconclusive",
}


async def _pending_task_ids(pool, limit: int) -> list[int]:
    rows = await pool.fetch(
        "SELECT id FROM tasks WHERE department = 'research' AND status = 'pending' ORDER BY id DESC LIMIT $1", limit
    )
    return [r["id"] for r in rows]


def _render(ctx: dict, refs, mimir, finding, grade: dict) -> None:
    print("\n" + "=" * 78)
    print(f"TASK T{ctx['task_id']} [{ctx['task_type']}] — {ctx['description'][:110]}")
    print(f"  direction: {ctx['direction'][:100]}")
    print(f"  expectation: {ctx['expectation'][:100]}")
    print(f"  kill-if:     {ctx['kill_condition'][:100]}")
    print("-" * 78)
    print(f"  search queries: {' | '.join(ctx.get('queries') or [])}")
    print(f"  retrieved {len(refs)} corpus passages · Mimir flagged {len(mimir.gaps)} gap(s)")
    print(
        f"\n  VERDICT: {_VERDICT_MARK.get(finding.verdict, finding.verdict)}  "
        f"(confidence {finding.confidence:.2f} · grounded {grade['grounded']:.0%} "
        f"of {grade['n_cited']} cited)"
    )
    print(f"  {finding.summary}")
    print(f"\n  kill-condition check: {finding.kill_condition_check}")
    if finding.key_evidence:
        print("  key evidence:")
        for t in finding.key_evidence[:5]:
            mark = "•" if t.strip().lower() in {(r.title or "").strip().lower() for r in refs} else "✗(unresolved)"
            print(f"    {mark} {t[:90]}")
    if finding.gaps:
        print("  gaps: " + "; ".join(finding.gaps[:4]))
    print(f"  next step → {finding.next_step[:140]}")


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set (and no .env) — cannot run.", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        state = PostgresClient(pool=pool)
        ids = await _pending_task_ids(pool, args.limit)
        if not ids:
            print("No pending research tasks — nothing to investigate.")
            return 0
        print(f"Researcher first light — {len(ids)} pending task(s), SHADOW (read-only, no writes)")
        tally: dict[str, int] = {}
        by_dir: dict = {}  # claim_id -> {"direction": str, "items": [FindingFeedback]}
        for tid in ids:
            try:
                result = await investigate_task(state, tid)  # emit=False → no events, no agent_runs
            except Exception as e:  # noqa: BLE001 — one bad task shouldn't sink the run
                print(f"\n  T{tid}: investigation failed — {e}")
                continue
            if result is None:
                continue
            ctx, refs, mimir, finding = result
            grade = grade_finding(finding, refs)
            _render(ctx, refs, mimir, finding, grade)
            tally[finding.verdict] = tally.get(finding.verdict, 0) + 1
            ff = finding_feedback(ctx, finding, grade["grounded"])
            slot = by_dir.setdefault(ctx["claim_id"], {"direction": ctx["direction"], "items": []})
            slot["items"].append(ff)

        # The FEEDBACK SEAM — what each direction's findings WOULD steer (shadow; nothing applied).
        print("\n" + "=" * 78)
        print("STEERING PLAN — what the feedback seam WOULD write (SHADOW · not applied)")
        print("=" * 78)
        for cid, slot in by_dir.items():
            fb = aggregate_direction(cid, slot["direction"], slot["items"])
            d = f"{fb.confidence_delta:+.3f}" if fb.confidence_delta else "0 (no decisive evidence)"
            print(f"\n  direction #{cid}: {slot['direction'][:84]}")
            print(
                f"    dominant: {_DISP_MARK.get(fb.dominant, fb.dominant)}  "
                f"·  Δconfidence {d}  ·  last_evidence_at {'→ now' if fb.set_last_evidence else 'unchanged'}"
            )
            print(
                "    per task: "
                + ", ".join(f"T{i.task_id} {_DISP_MARK.get(i.disposition, i.disposition)}" for i in fb.items)
            )
            if fb.acquire_queries:
                print(f"    would FIRE {len(fb.acquire_queries)} self-healing acquire(s):")
                for q in fb.acquire_queries:
                    print(f"      → {q[:80]}")

        print("\n" + "=" * 78)
        print("  SHADOW — nothing written (no confidence moved, no last_evidence_at, no acquires fired).")
        print("  verdict tally: " + (", ".join(f"{k} {v}" for k, v in tally.items()) or "none"))
        print("=" * 78 + "\n")
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.researcher_firstlight")
    ap.add_argument("--limit", type=int, default=8, help="max pending tasks to investigate")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
