"""
Bootstrap the LabFoundry research lab. Run once at the start of a research mandate.

Steps:
  1. Seed company_state with the research mandate / methodology / success-criterion.
  2. Invoke the PI in 'pi.frame_research' mode to generate 4-6
     candidate research questions.
  3. Insert each question as a claim with initial confidence (confidence=0.40).
  4. For each claim, queue knowledge acquisition tasks to ground it in literature.
  5. Emit 'company.bootstrapped' event so the harness begins the research loop.

Usage:
    python -m boardroom.bootstrap

Env:
    DATABASE_URL   postgres://user:pass@host:5432/labfoundry
    OLLAMA_URL     http://localhost:11434  (default)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from pydantic import BaseModel, Field

from boardroom.harness.curator import Curator
from boardroom.harness.router import Router, GPULock


# =========================================================================
# THE SEED — the only thing the watcher provides
# =========================================================================

SEED_PROBLEM = """\
Discover and execute a business that produces real revenue within 30 days,
starting from zero — no audience, no product, no domain commitment, no
hand-holding from the watcher. The output of the first weeks is a thesis
to commit to; by day 30 you must have shipped a deliverable to at least
one paying customer who is a stranger to the watcher.
"""

SEED_STANCE = """\
Pragmatist. Allergic to hype, MLM patterns, AI-generated SEO content,
dropshipping, affiliate-without-substance, and anything that would be
embarrassing to be publicly associated with. Pursue: real tools, real
intelligence, or real services that real people would actually pay for.
Prefer durable, compounding businesses over fast money. Quality is
non-negotiable; one mediocre product is worse than nothing.
"""

SEED_SUCCESS = """\
By day 30, at least one stranger (not the watcher) has committed to pay
for and received delivery of something the company produced. Commitment
is one of: paid invoice, charged payment, or signed contract with a
delivery date that fell within the 30-day window. The watcher provides
only infrastructure access (domain, payment processor account, hosting,
LLC paperwork if needed). The watcher does NOT make decisions, write
content, evaluate quality, or participate in the work. If the company
needs the watcher's judgment, the company has failed.
"""


# =========================================================================
# Output schema for ceo.exploration_kickoff
# =========================================================================

class CandidateCategory(BaseModel):
    claim: str = Field(
        ...,
        description="One sentence stating the category of business (not a specific product).",
    )
    rationale: str = Field(
        ...,
        description="2-3 sentences on why this is worth exploring, the rough economic logic, "
                    "and where differentiation might come from.",
    )
    risks: str = Field(
        ...,
        description="1-2 sentences on what would kill this category fast.",
    )
    disambiguating_questions: list[str] = Field(
        ...,
        min_length=3, max_length=3,
        description="Three specific questions whose answers tell us whether this category is real. "
                    "These become the first research tasks.",
    )


class ExplorationKickoffOutput(BaseModel):
    categories: list[CandidateCategory] = Field(
        ...,
        min_length=4, max_length=6,
        description="4-6 distinct, stance-compatible, timeline-compatible categories.",
    )
    selection_reasoning: str = Field(
        ...,
        description="Brief paragraph on why these categories were chosen and what space they cover.",
    )


# =========================================================================
# Bootstrap routine
# =========================================================================

async def bootstrap() -> None:
    db_url = os.environ["DATABASE_URL"]
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)

    try:
        # ---------- 1. seed company_state ----------
        deadline = datetime.now(timezone.utc) + timedelta(days=30)

        async with pool.acquire() as conn:
            existing = await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1")
            if existing:
                print("✗ Company already bootstrapped (company_state.id=1 exists). "
                      "Drop the row to re-bootstrap.")
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
        print(f"✓ Seeded company_state. Deadline: {deadline.isoformat()}")

        # ---------- 2. CEO exploration kickoff ----------
        # Wire up clients. In a fuller setup these come from a DI container.
        from boardroom.state.client import PostgresClient
        from boardroom.memory.client import ZepClient
        from boardroom.skills.client import LessonsClient

        state  = PostgresClient(pool=pool)
        memory = ZepClient.from_env()
        lessons = LessonsClient(pool=pool)

        curator = Curator(state=state, memory=memory, lessons=lessons)
        gpu_lock = GPULock()
        router = Router(pool=pool, gpu_lock=gpu_lock, ollama_url=ollama_url)

        print("→ Invoking CEO for exploration kickoff (workhorse tier)...")
        prompt = await curator.build("ceo.exploration_kickoff", context={})
        output, run_id = await router.invoke(
            prompt=prompt,
            output_schema_class=ExplorationKickoffOutput,
        )
        print(f"✓ CEO returned {len(output.categories)} categories (run #{run_id}).")

        # ---------- 3. Insert categories as theses ----------
        async with pool.acquire() as conn:
            async with conn.transaction():
                for cat in output.categories:
                    thesis_id = await conn.fetchval(
                        """
                        INSERT INTO theses (claim, confidence, created_by_run_id)
                        VALUES ($1, $2, $3)
                        RETURNING id
                        """,
                        cat.claim, 0.40, run_id,
                    )
                    # ---------- 4. Queue disambiguating questions ----------
                    for q in cat.disambiguating_questions:
                        await conn.execute(
                            """
                            INSERT INTO tasks (
                                thesis_id, department, task_type,
                                description, payload, priority
                            ) VALUES ($1, 'research', 'disambiguate', $2, $3::jsonb, 5)
                            """,
                            thesis_id,
                            q,
                            json.dumps({
                                "query": q,
                                "sources": ["web", "hacker_news", "reddit"],
                            }),
                        )
                    print(f"  ↳ T{thesis_id}: {cat.claim}")

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
                json.dumps({
                    "thesis_count": len(output.categories),
                    "run_id": run_id,
                    "deadline": deadline.isoformat(),
                }),
            )

        print()
        print("✓ Bootstrap complete.")
        print(f"  Theses seeded: {len(output.categories)}")
        print(f"  Research tasks queued: {len(output.categories) * 3}")
        print(f"  Deadline: {deadline.isoformat()}")
        print()
        print("  Selection reasoning from CEO:")
        print(f"    {output.selection_reasoning}")
        print()
        print("Start the harness now:")
        print("    python -m src.harness.main")

    finally:
        await router.close()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(bootstrap())
    except KeyboardInterrupt:
        sys.exit(130)
