"""
Planner handler — triggered by 'queue.empty' events for the research department.

When the research queue drains, the Planner generates 4-16 new tasks across
the active claims to keep the swarm fed.

Installs a 10-minute cooldown so multiple queue.empty events in rapid
succession (e.g., from concurrent task transitions) don't re-fire the Planner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class PlannedTask(BaseModel):
    claim_id: int
    task_type: Literal["disambiguate", "falsify", "deepen", "compare"]
    description: str = Field(..., max_length=300)
    query: str = Field(..., max_length=200)
    sources: list[Literal["web", "hacker_news", "reddit", "arxiv"]] = Field(
        default_factory=lambda: ["web", "hacker_news"],
    )
    priority: int = Field(default=5, ge=1, le=10)


class PlannedTasks(BaseModel):
    tasks: list[PlannedTask] = Field(..., max_length=20)
    reasoning: str = Field(
        ..., min_length=20,
        description="Brief: what space these tasks cover and why this batch now.",
    )


# -------------------------------------------------------------------------
# Task-data builder + recipe registration
# -------------------------------------------------------------------------

async def _build_planner_task_data(ctx: dict, state, memory) -> PromptLayer:
    claims, state_obj = await asyncio.gather(
        state.get_active_claims(limit=10),
        state.get_company_state(),
    )

    if not claims:
        content = """## Queue refill — no active claims

The research queue is empty AND there are no active claims to plan against.
This usually means: company is between phases, or all claims were killed.

Return an empty tasks list with reasoning explaining why no tasks were
created. The PI needs to spawn new claims before research can resume.
"""
        return PromptLayer(name="task_data", content=content, priority=1)

    findings_per_thesis = await asyncio.gather(*[
        state.get_recent_findings_for_claim(t.id, limit=5) for t in claims
    ])

    days_since_start = (datetime.now(timezone.utc) - state_obj.bootstrap_at).days

    blocks: list[str] = []
    for claim, findings in zip(claims, findings_per_thesis):
        if findings:
            f_lines = "\n".join(
                f"  - F{f.id} [rel {f.relevance_score}, audit={f.audit_verdict}]: {f.title}"
                for f in findings
            )
        else:
            f_lines = "  (no findings yet)"
        blocks.append(
            f"### T{claim.id} (conf {claim.confidence:.2f}): {claim.claim}\n{f_lines}"
        )

    content = f"""## Queue refill — research queue is empty

Phase: **{state_obj.current_phase}**  |  Days since start: {days_since_start}

## Active claims and recent findings

{chr(10).join(blocks)}

---

Generate 4-16 research tasks across these claims. Each is one unit of work
for a single Researcher invocation.

Per task:
  - `claim_id`:  which claim this serves
  - `task_type`:  disambiguate | falsify | deepen | compare
  - `description`: one sentence stating the question
  - `query`:      the actual search query
  - `sources`:    relevant sources
  - `priority`:   1-10 (default 5; raise for hot or under-explored claims)

Task type guidance:
  - `disambiguate`: claim is under-evidenced. Default in exploration phase.
  - `falsify`:      hunt for counter-evidence. Use when confidence >= 0.6.
  - `deepen`:       dig into a specific aspect surfaced by existing findings.
  - `compare`:      contrast with a sibling claim. Use in convergence phase.

Be specific in queries. "research the market" is useless. "Are there >10
active subreddits with >1000 members discussing problem X?" is concrete.

Distribute roughly evenly across claims unless one is clearly hotter.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "planner.generate_tasks" not in RECIPES:
    RECIPES["planner.generate_tasks"] = Recipe(
        invocation_type="planner.generate_tasks",
        description="Planner generates the next batch of research tasks.",
        agent="planner",
        total_budget=10_000,
        use_cold_path=False,
        recall_sessions=[],   # planner is task-and-state driven, not narrative
        recall_k=0,
        output_schema="PlannedTasks",
        task_data_builder=_build_planner_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_queue_empty(event: dict, dispatcher) -> Optional[dict]:
    """
    Triggered by queue.empty. Refills the research queue with planner tasks.
    Skips quickly for non-research department events.
    """
    payload = event.get("payload") or {}
    department = payload.get("department")
    if department != "research":
        return {"skipped": True, "reason": f"queue.empty for {department!r}, not research"}

    # 10-minute cooldown prevents thrash from rapid queue.empty fires
    await dispatcher.set_cooldown(
        invocation_type="planner.generate_tasks",
        target_type="queue",
        target_id=0,  # synthetic; queue.empty has no natural target id
        seconds=600,
    )

    # PLANNER_LOOP=v2 → assess_state → propose_tasks → critique
    # (boardroom.planner.loop). Highest-blast-radius rework so default off
    # until validated. The legacy single-shot path is unchanged below.
    impl = os.environ.get("PLANNER_LOOP", "v2").lower()
    if impl == "v2":
        from boardroom.planner.loop import run_planner_loop
        tasks, run_id, summary, confidence = await run_planner_loop(
            dispatcher=dispatcher,
            triggered_by_event_id=event["id"],
        )
        if not tasks:
            log.info("Planner v2 returned empty list: %s", summary)
            return {
                "tasks_created": 0,
                "run_id": run_id,
                "reasoning": summary,
                "critique_confidence": confidence,
            }

        async with dispatcher.pool.acquire() as conn:
            async with conn.transaction():
                for t in tasks:
                    await conn.execute(
                        """
                        INSERT INTO tasks (
                            claim_id, department, task_type,
                            description, payload, priority
                        )
                        VALUES ($1, 'research', $2, $3, $4::jsonb, $5)
                        """,
                        t.claim_id, t.task_type, t.description,
                        json.dumps({"query": t.query, "sources": t.sources}),
                        t.priority,
                    )

        return {
            "tasks_created": len(tasks),
            "run_id": run_id,
            "reasoning": summary,
            "critique_confidence": confidence,
        }

    # Legacy single-shot path.
    prompt = await dispatcher.curator.build(
        invocation_type="planner.generate_tasks",
        context={"department": department},
    )

    planned, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=PlannedTasks,
        triggered_by_event_id=event["id"],
    )

    if not planned.tasks:
        log.info("Planner returned empty list: %s", planned.reasoning)
        return {
            "tasks_created": 0,
            "run_id": run_id,
            "reasoning": planned.reasoning,
        }

    async with dispatcher.pool.acquire() as conn:
        async with conn.transaction():
            for t in planned.tasks:
                await conn.execute(
                    """
                    INSERT INTO tasks (
                        claim_id, department, task_type,
                        description, payload, priority
                    )
                    VALUES ($1, 'research', $2, $3, $4::jsonb, $5)
                    """,
                    t.claim_id, t.task_type, t.description,
                    json.dumps({"query": t.query, "sources": t.sources}),
                    t.priority,
                )

    return {
        "tasks_created": len(planned.tasks),
        "run_id": run_id,
        "reasoning": planned.reasoning,
    }
