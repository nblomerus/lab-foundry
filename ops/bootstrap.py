"""
Bootstrap the LabFoundry research lab. Run once at the start of a research mandate.

Steps:
  1. Seed company_state with the research mandate / methodology / success-criterion.
  2. Invoke the PI in 'pi.exploration_kickoff' mode to generate 4-6
     candidate research questions.
  3. Insert each question as a claim with initial confidence (confidence=0.40).
  4. For each claim, queue knowledge acquisition tasks to ground it in literature.
  5. Emit 'company.bootstrapped' event so the harness begins the research loop.

Usage:
    python -m ops.bootstrap

Env:
    DATABASE_URL   postgres://user:pass@host:5432/labfoundry
    OLLAMA_URL     http://localhost:11434  (default)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

import asyncpg
from pydantic import BaseModel, Field

from harness.curator import Curator
from harness.router import GPULock, Router, build_cloud_chain, build_premium_chain
from memory.client import ZepClient
from ops._env import load_dotenv
from skills.client import LessonsClient
from state.client import PostgresClient

# =========================================================================
# THE SEED — the only thing the watcher provides
# =========================================================================

SEED_PROBLEM = """\
Discover and rigorously investigate a promising, under-explored direction in
machine learning / AI research, using the Library (a large corpus of arXiv
papers) as the evidence base. There is no predetermined topic and no deadline:
self-discover where the leverage is, frame falsifiable claims, and establish
novel, well-supported findings — not a survey. The output is defensible
knowledge, earned against adversarial scrutiny.
"""

SEED_STANCE = """\
Demanding about rigour and evidence. Allergic to hype, hand-wavy claims,
p-hacking, cherry-picked benchmarks, and incremental deltas dressed as
breakthroughs. Pursue questions where a clear, falsifiable answer would
actually change how practitioners build systems. Quality of evidence over
volume of output; one well-supported finding is worth more than ten shallow
ones. A claim that cannot be tested is not worth making.
"""

SEED_SUCCESS = """\
A claim that survives adversarial scrutiny: reproducible evidence, a quantified
effect, honestly-stated limitations, and citations into the Library. Success is
a defensible finding — not a deadline met. There is no timeline; the lab is
judged on the rigour and novelty of what it establishes. The watcher provides
only infrastructure (compute, corpus, services) and does NOT make research
decisions, judge quality, or participate in the work.
"""


# =========================================================================
# Output schema for pi.exploration_kickoff
# =========================================================================


class CandidateCategory(BaseModel):
    claim: str = Field(
        ...,
        description="One sentence stating the research direction as a falsifiable thesis.",
    )
    rationale: str = Field(
        ...,
        description="2-3 sentences on why this matters and why it is under-explored or contested, "
        "and where the leverage is.",
    )
    risks: str = Field(
        ...,
        description="1-2 sentences on what would make this a dead end — already settled, not measurable, or confounded.",
    )
    disambiguating_questions: list[str] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Three specific questions whose answers tell us whether the direction is real "
        "and tractable. These become the first research tasks.",
    )


class ExplorationKickoffOutput(BaseModel):
    categories: list[CandidateCategory] = Field(
        ...,
        min_length=4,
        max_length=6,
        description="4-6 distinct, stance-compatible, falsifiable research directions.",
    )
    selection_reasoning: str = Field(
        ...,
        description="Brief paragraph on why these categories were chosen and what space they cover.",
    )


# =========================================================================
# Bootstrap routine
# =========================================================================


async def bootstrap() -> None:
    load_dotenv()  # so DATABASE_URL + the cloud keys load when run bare
    db_url = os.environ["DATABASE_URL"]
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)

    try:
        # ---------- 1. seed company_state ----------
        # No timeline: this is a research lab judged on rigour, not speed. The
        # deadline column is NOT NULL, so use a far-future placeholder — nothing
        # should apply deadline pressure.
        deadline = datetime.now(UTC) + timedelta(days=3650)

        async with pool.acquire() as conn:
            existing = await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1")
            if existing:
                print("✗ Company already bootstrapped (company_state.id=1 exists). Drop the row to re-bootstrap.")
                return

            await conn.execute(
                """
                INSERT INTO company_state (
                    id, problem_statement, stance, success_criterion, deadline
                ) VALUES (1, $1, $2, $3, $4)
                """,
                SEED_PROBLEM.strip(),
                SEED_STANCE.strip(),
                SEED_SUCCESS.strip(),
                deadline,
            )
        print("✓ Seeded company_state (research mandate, no timeline).")

        # ---------- 2. PI exploration kickoff ----------
        state = PostgresClient(pool=pool)
        memory = ZepClient.from_env()
        lessons = LessonsClient(pool=pool)

        curator = Curator(state=state, memory=memory, lessons=lessons)
        gpu_lock = GPULock()
        # Wire the cloud/premium chains so the WORKHORSE-tier kickoff uses DeepSeek
        # (cheap, reliable, and no contention with local Ollama) rather than the
        # local fallback.
        router = Router(
            pool=pool,
            gpu_lock=gpu_lock,
            ollama_url=ollama_url,
            cloud_chain=build_cloud_chain(os.environ),
            premium_chain=build_premium_chain(os.environ),
        )

        print("→ Invoking PI for exploration kickoff (workhorse tier)...")
        prompt = await curator.build("pi.exploration_kickoff", context={})
        output, run_id = await router.invoke(
            prompt=prompt,
            output_schema_class=ExplorationKickoffOutput,
        )
        print(f"✓ PI returned {len(output.categories)} categories (run #{run_id}).")

        # ---------- 3. Insert categories as claims ----------
        for cat in output.categories:
            claim = await state.create_claim(
                cat.claim,
                initial_confidence=0.40,
                created_by_run_id=run_id,
            )
            # ---------- 4. Queue disambiguating questions as research tasks ----------
            async with pool.acquire() as conn, conn.transaction():
                for q in cat.disambiguating_questions:
                    await conn.execute(
                        """
                            INSERT INTO tasks (
                                claim_id, department, task_type,
                                description, payload, priority
                            ) VALUES ($1, 'research', 'disambiguate', $2, $3::jsonb, 5)
                            """,
                        claim.id,
                        q,
                        json.dumps(
                            {
                                "query": q,
                                "sources": ["web", "hacker_news", "reddit"],
                            }
                        ),
                    )
            print(f"  ↳ C{claim.id}: {cat.claim}")

        # ---------- 5. Emit bootstrap event ----------
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (event_type, target_type, payload, dedup_key)
                VALUES (
                    'company.bootstrapped',
                    'company',
                    $1::jsonb,
                    'bootstrap-1'
                )
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                json.dumps(
                    {
                        "claim_count": len(output.categories),
                        "run_id": run_id,
                        "deadline": deadline.isoformat(),
                    }
                ),
            )

        print()
        print("✓ Bootstrap complete.")
        print(f"  Claims seeded: {len(output.categories)}")
        print(f"  Research tasks queued: {len(output.categories) * 3}")
        print("  Timeline: none (research lab)")
        print()
        print("  Selection reasoning from PI:")
        print(f"    {output.selection_reasoning}")
        print()
        print("Start the harness now:")
        print("    python -m harness.main")

    finally:
        await router.close()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(bootstrap())
    except KeyboardInterrupt:
        sys.exit(130)
