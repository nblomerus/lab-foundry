"""
ops.ariadne_reflect — run Ariadne's REFLECT & STEER pass once, read-only.

Mirrors ops.ariadne_firstlight but for the feedback half of her loop: she reads the
STANDING agenda (directions + scores + goals + lifecycle) against the CURRENT field
model + standing lessons, and prints her per-direction verdicts (advance/reprioritize/
pivot/retire) + the strategic lessons she'd record. Writes NOTHING — no claim changes,
no lessons. Use it to preview what an advisory reflect would do before flipping her on.

    set -a; . ./.env; set +a
    python -m ops.ariadne_reflect

Needs DATABASE_URL (+ DEEPSEEK_API_KEY, else falls back to local Ollama). Read-only.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from agents.ariadne.grade import grade_reflection
from agents.ariadne.loop import ARIADNE_MODEL
from agents.ariadne.reflect import run_reflection
from state.client import PostgresClient


def _h(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


_MARK = {"advance": "→ ADVANCE", "reprioritize": "↕ REPRIORITIZE", "pivot": "⟳ PIVOT", "retire": "✗ RETIRE"}


async def run() -> int:
    load_dotenv()
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not set (and no .env) — cannot run.", file=sys.stderr)
        return 2
    if not os.environ.get("DEEPSEEK_API_KEY"):
        # Lab policy: DeepSeek (cloud) or local Ollama only — without a key the chain falls back to local.
        print("DEEPSEEK_API_KEY not set — Ariadne will fall back to the local Ollama model.", file=sys.stderr)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    try:
        state = PostgresClient(pool=pool)
        print(f"Ariadne reflect — model={ARIADNE_MODEL}  (shadow / read-only)")
        out, valid_ids = await run_reflection(state)
        if out is None:
            print("\nNo standing directions to steer — frame an agenda first (ariadne.deliberate).")
            return 0

        _h("ARIADNE — reflect & steer (standing agenda × current landscape)")
        print(f"\nPORTFOLIO\n  {out.portfolio_assessment}\n")
        for v in out.verdicts:
            mark = _MARK.get(v.assessment, v.assessment)
            pr = f"  →priority={v.new_priority}" if v.new_priority else ""
            print(f"  {mark}  direction #{v.claim_id}{pr}")
            print(f"      {v.reason}")
        if out.lessons:
            print("\nSTRATEGIC LESSONS (would land probationary; fed back into deliberation)")
            for les in out.lessons:
                when = f"  (when: {les.applies_when})" if les.applies_when else ""
                print(f"  - {les.lesson}{when}")
        print(f"\nNEXT FOCUS\n  {out.reprioritized_focus}")
        print("\n" + "-" * 78)
        print("  SHADOW MODE — read-only. No claims steered, no lessons written.")
        print("-" * 78)

        r = grade_reflection(out, valid_ids)
        _h("GRADES (reflection)")
        ok = "✓"
        print(
            f"  {ok if r.verdicts_valid == 1.0 else '✗'} verdicts reference real standing ids "
            f"(valid assessment): {r.verdicts_valid:.0%} ({r.n_verdicts} verdicts)"
        )
        if r.invalid_refs:
            print(f"    invalid refs (hallucinated ids): {r.invalid_refs}")
        print(f"  · {r.n_lessons} strategic lesson(s)")
        print(
            "\n  " + ("✓ PASS — eligible to persist (advisory/active)." if r.passed else "✗ NOT yet — see flags above.")
        )
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
