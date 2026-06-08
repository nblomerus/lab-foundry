"""
ops.planner_firstlight — run the Planner once, read-only (Stage 2 shadow).

Mirrors ops.ariadne_firstlight. Reads Ariadne's APPROVED directions + their goals and
prints the research tasks it WOULD create for the Researcher — decomposed and hardware-
fit — without writing anything. Use it to preview a plan before flipping the planner on.

    set -a; . ./.env; set +a
    python -m ops.planner_firstlight        # needs at least one approved direction on /ariadne

Needs DATABASE_URL + DEEPSEEK_API_KEY. Read-only.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from agents.planner.plan import grade_plan, run_planning
from state.client import PostgresClient


async def run() -> int:
    load_dotenv()
    if not os.environ.get("DATABASE_URL") or not os.environ.get("DEEPSEEK_API_KEY"):
        print("DATABASE_URL and DEEPSEEK_API_KEY required.", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    try:
        state = PostgresClient(pool=pool)
        print("Planner first light — shadow / read-only")
        out, ids = await run_planning(state)
        if out is None:
            print(
                "\nNo APPROVED directions to plan. Approve a direction on /ariadne (or POST "
                '/ariadne/gate/<id> {"decision":"approved"}) first.'
            )
            return 0

        print("\n" + "=" * 78 + "\nPLANNER — tasks per approved direction\n" + "=" * 78)
        for p in out.plans:
            print(f"\nDIRECTION #{p.claim_id}  →  {len(p.tasks)} task(s):")
            for t in p.tasks:
                print(f"  [{t.priority}/{t.task_type}] {t.title}")
                print(f"      {t.description[:170]}")
                if t.rationale:
                    print(f"      ↳ {t.rationale[:120]}")
        if out.notes:
            print(f"\nNOTES\n  {out.notes}")
        print("\n" + "-" * 78 + "\n  SHADOW — read-only. No tasks written.\n" + "-" * 78)

        g = grade_plan(out, ids)
        ok = "✓"
        print("\nGRADE:")
        print(f"  {ok if g.valid_refs else '✗'} plans reference real approved directions")
        print(
            f"  {ok if g.tasks_wellformed else '✗'} tasks well-formed (description + known type): "
            f"{g.n_tasks} tasks across {g.n_plans} direction(s)"
        )
        if g.invalid_refs:
            print(f"    invalid refs (hallucinated ids): {g.invalid_refs}")
        print(
            "\n  "
            + ("✓ PASS — eligible to create tasks (advisory/active)." if g.passed else "✗ NOT yet — see flags above.")
        )
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
