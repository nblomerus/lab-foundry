"""
Planner handler — triggered by 'queue.empty' events for the research department.

When the research queue drains, the Planner generates 4-16 new tasks across
the active theses to keep the swarm fed.

Installs a 10-minute cooldown so multiple queue.empty events in rapid
succession (e.g., from concurrent task transitions) don't re-fire the Planner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class PlannedTask(BaseModel):
    thesis_id: int
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
    theses, state_obj = await asyncio.gather(
        state.get_active_theses(limit=10),
        state.get_company_state(),
    )

    if not theses:
        content = """## Queue refill — no active theses

The research queue is empty AND there are no active theses to plan against.
This usually means: company is between phases, or all theses were killed.

Return an empty tasks list with reasoning explaining why no tasks were
created. The CEO needs to spawn new theses before research can resume.
"""
        return PromptLayer(name="task_data", content=content, priority=1)

    findings_per_thesis = await asyncio.gather(*[
        state.get_recent_findings_for_thesis(t.id, limit=5) for t in theses
    ])

    days_remaining = (state_obj.deadline - datetime.now(timezone.utc)).days

    blocks: list[str] = []
    for thesis, findings in zip(theses, findings_per_thesis):
        if findings:
            f_lines = "\n".join(
                f"  - F{f.id} [rel {f.relevance_score}, audit={f.audit_verdict}]: {f.title}"
                for f in findings
            )
        else:
            f_lines = "  (no findings yet)"
        blocks.append(
            f"### T{thesis.id} (conf {thesis.confidence:.2f}): {thesis.claim}\n{f_lines}"
        )

    content = f"""## Queue refill — research queue is empty

Phase: **{state_obj.current_phase}**  |  Days remaining: {days_remaining}

## Active theses and recent findings

{chr(10).join(blocks)}

---

Generate 4-16 research tasks across these theses. Each is one unit of work
for a single Researcher invocation.

Per task:
  - `thesis_id`:  which thesis this serves
  - `task_type`:  disambiguate | falsify | deepen | compare
  - `description`: one sentence stating the question
  - `query`:      the actual search query
  - `sources`:    relevant sources
  - `priority`:   1-10 (default 5; raise for hot or under-explored theses)

Task type guidance:
  - `disambiguate`: thesis is under-evidenced. Default in exploration phase.
  - `falsify`:      hunt for counter-evidence. Use when confidence >= 0.6.
  - `deepen`:       dig into a specific aspect surfaced by existing findings.
  - `compare`:      contrast with a sibling thesis. Use in convergence phase.

Be specific in queries. "research the market" is useless. "Are there >10
active subreddits with >1000 members discussing problem X?" is concrete.

Distribute roughly evenly across theses unless one is clearly hotter.
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
                        thesis_id, department, task_type,
                        description, payload, priority
                    )
                    VALUES ($1, 'research', $2, $3, $4::jsonb, $5)
                    """,
                    t.thesis_id, t.task_type, t.description,
                    json.dumps({"query": t.query, "sources": t.sources}),
                    t.priority,
                )

    return {
        "tasks_created": len(planned.tasks),
        "run_id": run_id,
        "reasoning": planned.reasoning,
    }
