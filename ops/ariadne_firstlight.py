"""
Ariadne first light — run the research PI once, in SHADOW mode, read-only.

Mirrors ops/mimir_firstlight.py: set up state + read the trusted substrate, run one
deliberation, and PRINT the grounded direction tree. Writes nothing — no claims, no
claim_goals, no events. This is the readiness plan's first shadow-mode step: prove
Ariadne produces a grounded, structured agenda from the seed problem + the substrate.

    set -a; . ./.env; set +a
    python -m ops.ariadne_firstlight            # uses company_state.problem_statement
    python -m ops.ariadne_firstlight --seed "..."   # override the seed problem

Needs DATABASE_URL + OLLAMA_URL (retrieval embeds the query); the LLM runs via DEEPSEEK_API_KEY
(or falls back to local Ollama).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from agents.ariadne.grade import grade
from agents.ariadne.loop import ARIADNE_MODEL, run_shadow
from agents.ariadne.scoring import composite, priority_label
from state.client import PostgresClient


def _h(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def _render(out) -> None:
    _h("ARIADNE — shadow direction tree (ranked by decision-framework composite)")
    print(f"\nMISSION\n  {out.mission_frame}\n")
    ranked = sorted(out.directions, key=lambda d: composite(d.scores) if d.scores else 0.0, reverse=True)
    for rank, d in enumerate(ranked, 1):
        print(f"DIRECTION (priority #{rank}): {d.title}")
        if d.scores:
            s = d.scores
            print(
                f"  decision: [{priority_label(composite(s)).upper()} · {composite(s)}/5]  "
                f"nov{s.novelty} dif{s.differentiation} pap{s.paper_potential} fea{s.feasibility} "
                f"evd{s.evidence_availability} rev{s.reviewer_interest} dep{s.technical_depth} "
                f"cost{s.cost_efficiency} lab{s.lab_alignment}"
            )
            if s.rationale:
                print(f"            {s.rationale}")
        print(f"  bet:     {d.statement}")
        print(f"  novelty: {d.novelty_rationale}")
        for g in d.claim_goals:
            print(f"    · goal: expect={g.expectation}")
            print(f"            kill={g.kill_condition}")
            if g.novelty_target:
                print(f"            novelty_target={g.novelty_target}")
            if g.next_milestone:
                print(f"            next={g.next_milestone}  [{g.priority_hint or '—'}]")
        if d.kill_conditions:
            print(f"  kill conditions: {'; '.join(d.kill_conditions)}")
        if d.reviewer_risks:
            print(f"  reviewer risks:  {'; '.join(d.reviewer_risks)}")
        print()
    if out.novelty_risks:
        print("NOVELTY/SATURATION RISKS")
        for r in out.novelty_risks:
            print(f"  - {r}")
    if out.requests:
        print("\nREQUESTS TO MIMIR (fetch a SPECIFIC missing paper — not topics)")
        for r in out.requests:
            tag = f" [{r.arxiv_id}]" if r.arxiv_id else ""
            print(f"  - {r.paper}{tag}  ({r.why})")
    print(f"\nREFLECTION\n  {out.reflection}")
    print("\n" + "-" * 78)
    print("  SHADOW MODE — read-only. Nothing was written (no claims, claim_goals, or events).")
    print("-" * 78 + "\n")


def _render_grades(r) -> None:
    _h("GRADES (readiness Stage 7 — checkable predicates)")
    ok = "✓"
    print(f"  {ok if r.schema_valid else '✗'} schema valid (≥3 directions, each with novelty + goals)")
    print(f"  {ok if r.claim_goals_wellformed == 1.0 else '⚠'} claim_goals well-formed: {r.claim_goals_wellformed:.0%}")
    print(
        f"  {ok if r.directions_grounded == 1.0 else '⚠'} directions grounded (cite ≥1 work): {r.directions_grounded:.0%}"
    )
    print(
        f"  {ok if r.citations_resolved >= 0.8 else '✗'} citations resolve to certified corpus docs: "
        f"{r.citations_resolved:.0%} ({r.n_citations} cited)"
    )
    print(
        f"  {ok if r.scores_wellformed == 1.0 else '✗'} decision scores well-formed "
        f"(all 9 dims 1–5): {r.scores_wellformed:.0%}"
    )
    if r.unresolved:
        print("    unresolved (possible hallucinations):")
        for u in r.unresolved:
            print(f"      - {u!r}")
    print("\n  " + ("✓ PASS — eligible for advisory mode." if r.passed else "✗ NOT yet — see flags above."))


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set (and no .env) — cannot run.", file=sys.stderr)
        return 2
    if not os.environ.get("DEEPSEEK_API_KEY"):
        # Lab policy: DeepSeek (cloud) or local Ollama only. Without a DeepSeek key the chain
        # falls back to the local model — warn, don't block.
        print("DEEPSEEK_API_KEY not set — Ariadne will fall back to the local Ollama model.", file=sys.stderr)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        state = PostgresClient(pool=pool)
        if args.seed:  # override the seed problem in-memory only (still writes nothing)
            cs = await state.get_company_state()
            cs.problem_statement = args.seed
            state.get_company_state = lambda: _async_return(cs)  # type: ignore[assignment]
        print(f"Ariadne first light — model={ARIADNE_MODEL}  (shadow / read-only)")
        out = await run_shadow(state)
        _render(out)
        report = await grade(out)
        _render_grades(report)
    finally:
        await pool.close()
    return 0


async def _async_return(v):
    return v


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.ariadne_firstlight")
    ap.add_argument("--seed", default=None, help="override the seed problem (in-memory; still read-only)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
