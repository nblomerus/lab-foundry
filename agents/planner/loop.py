"""
Planner loop — three-step deliberation replacing the single-shot
`planner.generate_tasks`.

Pipeline per queue.empty event:

    assess_state(theses, recent_findings, phase, deadline)
        → StateAssessment (per-thesis gaps, target task count)

    propose_tasks(assessment)
        → PlannedTasks (initial batch — same shape as legacy)

    critique(assessment, proposal)
        → CritiquedTasks (final list after self-review)

The commit-to-DB transaction at the end is unchanged from the legacy
handler. The point of the rework is the **critique** step — the legacy
planner has no opportunity to second-guess its own list before writing
4-16 tasks straight to the tasks table.

Behind `PLANNER_LOOP=v2`. Default off. Planner has the highest blast
radius of all the agent reworks (a regression here poisons the entire
swarm silently for 10+ minutes), so it stays opt-in until validated by a
shadow-run comparison against the legacy path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from agents.planner.handler import PlannedTask, PlannedTasks
from agents.planner.schemas import (
    CritiquedTasks,
    StateAssessment,
)
from harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Prompt builders
# -------------------------------------------------------------------------


async def _build_assess_state(ctx: dict, state, memory) -> PromptLayer:
    theses, state_obj = await asyncio.gather(
        state.get_active_theses(limit=10),
        state.get_company_state(),
    )

    findings_per_thesis = await asyncio.gather(*[state.get_recent_findings_for_thesis(t.id, limit=8) for t in theses])

    days_since_start = (datetime.now(UTC) - state_obj.bootstrap_at).days

    blocks: list[str] = []
    for thesis, findings in zip(theses, findings_per_thesis, strict=False):
        if findings:
            f_lines = "\n".join(
                f"  - F{f.id} [rel {f.relevance_score}, audit={f.audit_verdict}, "
                f"supports={f.supports_thesis}]: {(f.title or '')[:80]}"
                for f in findings
            )
        else:
            f_lines = "  (no findings yet)"
        blocks.append(f"### T{thesis.id} (conf {thesis.confidence:.2f}): {thesis.claim}\n{f_lines}")

    content = f"""## Assess the research portfolio

Phase: **{state_obj.current_phase}**  |  Days since start: {days_since_start}

The research queue is empty. Before proposing tasks, audit the portfolio:
where is evidence thin, where is it concentrated, what's the most
load-bearing gap?

## Active theses and recent findings

{chr(10).join(blocks) if blocks else "(no active theses)"}

---

For EACH active thesis above, emit a `ThesisGap`:

- `thesis_id`: the T-id
- `evidence_gap`: one sentence — what's *concretely* missing? Bad: "needs
  more research". Good: "no quantitative comparison vs. the 3 named
  competitors", "no actual user complaints surfaced yet".
- `suggested_task_type`:
    * `disambiguate` for under-evidenced theses (default in exploration)
    * `falsify` when confidence ≥ 0.6 — hunt for counter-evidence
    * `deepen` when an angle in existing findings begs a follow-up
    * `compare` for convergence-phase pairs that should be contrasted
- `priority_score` (0..1): 0 = skip this batch (saturated or stalled),
  0.5 = normal, 1.0 = work this next.

Then summarize the portfolio in `portfolio_notes` (1-2 sentences):
where is research over-concentrated, where are the blind spots?

Finally `target_task_count`: 4-16. Use 4-6 for a saturated portfolio
(most theses high-conf with thick evidence), 12-16 for thin/early state.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_propose_tasks(ctx: dict, state, memory) -> PromptLayer:
    state_obj = ctx["state"]
    assessment: StateAssessment = ctx["assessment"]
    theses_by_id = ctx["theses_by_id"]  # dict[int, Thesis]

    gap_lines: list[str] = []
    for g in assessment.thesis_gaps:
        thesis = theses_by_id.get(g.thesis_id)
        claim = thesis.claim if thesis is not None else "(unknown)"
        gap_lines.append(
            f"### T{g.thesis_id} (priority {g.priority_score:.2f}, "
            f"suggested {g.suggested_task_type})\n"
            f"Claim: {claim}\n"
            f"Gap: {g.evidence_gap}"
        )

    content = f"""## Propose research tasks from the assessment

Phase: **{state_obj.current_phase}**

## Portfolio assessment

**Notes:** {assessment.portfolio_notes}

**Target task count for this batch:** {assessment.target_task_count}

## Per-thesis gaps

{chr(10).join(gap_lines) if gap_lines else "(no gaps — emit empty list)"}

---

Generate ~{assessment.target_task_count} research tasks. **Skip any thesis
whose priority_score is 0.0** — the assessment said don't work it now.
Concentrate effort on the high-priority gaps; distribute roughly in
proportion to priority_score.

Per task:
  - `thesis_id`: which thesis this serves
  - `task_type`: align with the assessment's `suggested_task_type` for that
    thesis unless you can name a reason to differ
  - `description`: one sentence — the specific question, not a topic
  - `query`: the actual search query
  - `sources`: relevant sources (web/hacker_news/reddit/arxiv)
  - `priority`: 1-10 (default 5; raise for hot theses)

Be specific in queries. "research the market" is useless. "Are there >10
active subreddits with >1000 members discussing problem X?" is concrete.

`reasoning`: brief — what space these tasks cover, why this batch now.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_critique(ctx: dict, state, memory) -> PromptLayer:
    assessment: StateAssessment = ctx["assessment"]
    proposal: PlannedTasks = ctx["proposal"]
    theses_by_id = ctx["theses_by_id"]

    proposal_lines: list[str] = []
    for i, t in enumerate(proposal.tasks):
        thesis = theses_by_id.get(t.thesis_id)
        claim_snip = (thesis.claim if thesis else "(unknown)")[:80]
        proposal_lines.append(
            f"[{i}] T{t.thesis_id} ({t.task_type}, pri {t.priority}): {t.description}\n"
            f"    query:   {t.query}\n"
            f"    sources: {t.sources}\n"
            f"    thesis:  {claim_snip}"
        )

    gap_lines: list[str] = []
    for g in assessment.thesis_gaps:
        gap_lines.append(
            f"  T{g.thesis_id}: gap='{g.evidence_gap}', suggested={g.suggested_task_type}, pri={g.priority_score:.2f}"
        )

    content = f"""## Critique the proposed task batch

The assessment said where the portfolio's gaps are. The propose step
generated this list. **Now critique it before it hits the swarm.**

## Assessment recap

Target count: {assessment.target_task_count}
Portfolio notes: {assessment.portfolio_notes}

Per-thesis gaps:
{chr(10).join(gap_lines)}

## Proposed tasks ({len(proposal.tasks)})

{chr(10).join(proposal_lines) if proposal_lines else "(empty)"}

Original proposal reasoning: {proposal.reasoning}

---

Emit the FINAL task list (`final_tasks`) after self-review. Common failure
modes to catch:

- **Duplicates / near-duplicates** — two tasks asking the same question
  from different angles that won't yield different answers. Drop one.
- **Wrong task type for the gap** — proposal says `disambiguate` but the
  gap calls for `falsify` (confidence already high), or vice versa.
- **Off-thesis tasks** — a task whose query doesn't actually probe the
  named gap. Drop or rewrite.
- **Vague queries** — "research the X market" with no specifics. Rewrite
  or drop.
- **Mis-distributed** — too many tasks on a priority-0.3 thesis while a
  priority-0.9 one only got one.
- **Over-count** — proposal exceeds target_task_count by >2. Trim the
  lowest-value items.

You may KEEP a task as-is, EDIT it (same thesis_id but different
query/description), or DROP it. `final_tasks` is the post-critique list
that will be committed.

`changes_summary` (2-4 sentences): what was kept/dropped/edited and why.
`confidence` (0..1): how confident are you this list is right? Low
confidence is honest — the swarm runs it either way, but trace observers
can spot weak batches.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe registration
# -------------------------------------------------------------------------

_PLANNER_RECIPES: list[tuple[str, str, int, str, callable]] = [
    (
        "planner.assess_state",
        "Audit the research portfolio: per-thesis evidence gaps + target task count.",
        8_000,
        "StateAssessment",
        _build_assess_state,
    ),
    (
        "planner.propose_tasks",
        "Generate the candidate task batch from the assessment.",
        10_000,
        "PlannedTasks",
        _build_propose_tasks,
    ),
    (
        "planner.critique",
        "Self-review the proposed task batch and emit the final list.",
        10_000,
        "CritiquedTasks",
        _build_critique,
    ),
]

for _itype, _desc, _budget, _schema, _builder in _PLANNER_RECIPES:
    if _itype not in RECIPES:
        RECIPES[_itype] = Recipe(
            invocation_type=_itype,
            description=_desc,
            agent="planner",
            total_budget=_budget,
            use_cold_path=False,
            recall_sessions=[],
            recall_k=0,
            output_schema=_schema,
            task_data_builder=_builder,
        )


# -------------------------------------------------------------------------
# Orchestrator
# -------------------------------------------------------------------------


async def run_planner_loop(
    *,
    dispatcher,
    triggered_by_event_id: int | None = None,
) -> tuple[list[PlannedTask], int, str, float]:
    """
    Three-step planner. Returns (final_tasks, critique_run_id,
    changes_summary, critique_confidence). Caller writes the tasks to DB
    in a single transaction — same as the legacy path.
    """
    router = dispatcher.router
    curator = dispatcher.curator
    state = dispatcher.state
    session = dispatcher.session

    # ---- 1. assess_state ------------------------------------------------
    assess_prompt = await curator.build(
        invocation_type="planner.assess_state",
        context={"department": "research"},
    )
    assessment, assess_run_id = await router.invoke(
        prompt=assess_prompt,
        output_schema_class=StateAssessment,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="assess_state",
    )

    if not assessment.thesis_gaps:
        # No active theses; nothing to plan.
        return [], assess_run_id, "no active theses", 1.0

    theses = await state.get_active_theses(limit=10)
    theses_by_id = {t.id: t for t in theses}
    state_obj = await state.get_company_state()

    # ---- 2. propose_tasks -----------------------------------------------
    propose_prompt = await curator.build(
        invocation_type="planner.propose_tasks",
        context={
            "assessment": assessment,
            "theses_by_id": theses_by_id,
            "state": state_obj,
        },
    )
    proposal, propose_run_id = await router.invoke(
        prompt=propose_prompt,
        output_schema_class=PlannedTasks,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="propose_tasks",
        parent_step_id=assess_run_id,
    )

    if not proposal.tasks:
        return [], propose_run_id, proposal.reasoning, 1.0

    # ---- 3. critique ----------------------------------------------------
    critique_prompt = await curator.build(
        invocation_type="planner.critique",
        context={
            "assessment": assessment,
            "proposal": proposal,
            "theses_by_id": theses_by_id,
        },
    )
    critiqued, critique_run_id = await router.invoke(
        prompt=critique_prompt,
        output_schema_class=CritiquedTasks,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="critique",
        parent_step_id=propose_run_id,
    )

    # Guard against critique returning tasks for theses that aren't active
    # (or other edge cases): filter to known thesis ids.
    final = [t for t in critiqued.final_tasks if t.thesis_id in theses_by_id]
    if len(final) < len(critiqued.final_tasks):
        log.warning(
            "planner critique returned %d tasks against unknown theses; filtered to %d",
            len(critiqued.final_tasks) - len(final),
            len(final),
        )

    return final, critique_run_id, critiqued.changes_summary, critiqued.confidence
