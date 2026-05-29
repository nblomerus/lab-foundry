"""
Reflection handler — triggered by 'reflection.requested' events.

The 002_skills.sql trigger fires reflection.requested whenever an agent run
completes that was associated with dissent (audit slop, critic kill, or
the run itself being a critic that produced a non-pass verdict).

This handler decides whether the run yields a generalizable lesson. Most
dissents do NOT — the bias is heavily toward "no lesson." Only patterns
that will recur become lessons; one-off mistakes don't.

When a lesson is proposed, it lands as 'probationary' in the lessons table.
The reconcile_lessons() function promotes to 'active' after 5 supportive
applications, or retires after 3 contradicting applications.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from pydantic import BaseModel, Field

from boardroom.harness.curator import RECIPES, PromptLayer, Recipe

log = logging.getLogger(__name__)


async def _persist_or_credit_lesson(dispatcher, cand, derived_from_run_id: int) -> Optional[int]:
    """
    Persist a proposed lesson with scope-hygiene + dedupe (Phase 0):

      * scope-hygiene — a lesson scoped to an invocation type no recipe registers
        can never be injected (e.g. a renamed/dead name like `critic.kill_verdict`);
        drop it rather than store dead advice.
      * dedupe — if a near-duplicate already exists for this invocation, credit
        the original with a recurrence (promotion pressure) instead of inserting.

    Returns the lesson id touched (new or existing), or None if dropped.
    """
    if cand.applies_to_invocation not in RECIPES:
        log.info("reflection: dropping lesson scoped to unregistered invocation %r",
                 cand.applies_to_invocation)
        return None

    dup = await dispatcher.lessons.find_near_duplicate(
        cand.applies_to_invocation, cand.lesson_text,
    )
    if dup is not None:
        await dispatcher.lessons.credit_recurrence(dup, derived_from_run_id)
        log.info("reflection: lesson near-dup of #%d — credited recurrence, not inserted", dup)
        return dup

    return await dispatcher.lessons.insert_lesson_candidate(
        invocation_type=cand.applies_to_invocation,
        applies_when=cand.applies_when,
        lesson_text=cand.lesson_text,
        rationale=cand.rationale,
        derived_from_run_id=derived_from_run_id,
        derived_via="reflection",
    )


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------

class LessonCandidate(BaseModel):
    lesson_text:           str = Field(..., min_length=20, max_length=400,
                                       description="Short, generalizable heuristic.")
    applies_to_invocation: str = Field(..., description="invocation_type the lesson applies to.")
    applies_when:          dict = Field(default_factory=dict,
                                        description="Predicate matched against context dict. {} = always.")
    rationale:             str = Field(..., min_length=20)


class ReflectionOutput(BaseModel):
    should_create_lesson: bool
    candidate:           Optional[LessonCandidate] = None
    reasoning:           str = Field(..., min_length=10)


# v2 (REFLECTION_LOOP=v2): one LLM call sees a *batch* of recent dissenting
# runs and looks for patterns ACROSS them. Lessons that show up once aren't
# patterns; lessons that show up three times are.
class BatchReflectionOutput(BaseModel):
    lessons: list[LessonCandidate] = Field(
        default_factory=list,
        description="0-3 lessons that recur across the batch. Empty is the correct answer when no pattern is visible.",
    )
    reasoning: str = Field(..., min_length=10,
                           description="One sentence per emitted lesson, or 'no recurring pattern' if none.")


# -------------------------------------------------------------------------
# Task-data builder + recipe
# -------------------------------------------------------------------------

async def _build_reflection_task_data(ctx: dict, state, memory) -> PromptLayer:
    invocation_type = ctx["invocation_type"]
    run_summary     = ctx["run_summary"]

    content = f"""## Reflection on a dissenting run

A run just completed that involved dissent (audit slop, critic kill, or
critic non-pass). Decide whether a generalizable lesson can be drawn.

## The run
**Invocation:** {invocation_type}
**Output summary:** {run_summary}

---

A good lesson is:
  - **Specific enough to apply** to a future run
  - **General enough to recur** — not just describing this one run
  - **Falsifiable**: future runs will validate or contradict it

Good examples:
  - "Findings citing AI-generated newsletters often score 8+ but yield
    low-quality claims — discount them by 2 points."
  - "Theses depending on 'enterprises will need X' fail when X already has
    competitors — always check the existing-solutions landscape first."

Bad examples (do NOT write these):
  - "Always be more careful." (vague)
  - "Don't propose claim T17 again." (too specific)
  - "Try harder." (worthless)

Bias is toward should_create_lesson=FALSE. Most dissents are one-off
mistakes, not repeatable patterns. Only say true when the pattern will
predictably recur AND a future agent could act on the lesson.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "reflect.lesson_propose" not in RECIPES:
    RECIPES["reflect.lesson_propose"] = Recipe(
        invocation_type="reflect.lesson_propose",
        description="Decide whether a dissenting run yields a generalizable lesson.",
        agent="evaluation",   # reuse: same skeptical mindset as slop detection
        total_budget=5_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ReflectionOutput",
        task_data_builder=_build_reflection_task_data,
    )


# v2 batch prompt: read N recent dissents and surface lessons that RECUR.
async def _build_batch_reflection_task_data(ctx: dict, state, memory) -> PromptLayer:
    runs = ctx["runs"]  # list[dict]: {id, invocation_type, output_summary, started_at}
    if not runs:
        body = "(no dissents in the window — emit empty lessons list)"
    else:
        blocks = []
        for r in runs:
            blocks.append(
                f"### Run #{r['id']} ({r['invocation_type']}, {r['ago']})\n"
                f"{(r['output_summary'] or '(no summary)')[:1200]}"
            )
        body = "\n\n".join(blocks)

    content = f"""## Reflection across recent dissenting runs

You see {len(runs)} runs from the last batch window. Each is a run that
involved dissent (audit slop, critic kill, critic non-pass).

{body}

---

Find **recurring** patterns. A lesson is only worth proposing when:

- It appears in **≥ 2** runs above (one-off mistakes don't generalize), AND
- It's **specific enough** for a future agent to act on, AND
- It's **falsifiable** — a future run will validate or contradict it.

For each lesson, set:
  - `applies_to_invocation`: the invocation_type the lesson targets
    (e.g. `researcher.synthesize`, `critic.judge_verdict`).
  - `applies_when`: a small predicate dict, or {{}} for always.
  - `lesson_text`: the heuristic. Imperative voice. ≤ 1 sentence.
  - `rationale`: which 2+ runs above show the pattern, briefly.

Emit at most 3 lessons. **Empty list is the correct answer** when no pattern
recurs — most batches should be empty. `reasoning`: one sentence per lesson,
or "no recurring pattern" if none.

Bad (do NOT emit):
  - "Be more careful" (vague, unfalsifiable)
  - "Don't kill T17" (too specific)
  - A lesson visible in only 1 run (not recurring)
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "reflect.batch_propose_lessons" not in RECIPES:
    RECIPES["reflect.batch_propose_lessons"] = Recipe(
        invocation_type="reflect.batch_propose_lessons",
        description="Find lessons that recur across a batch of recent dissents.",
        agent="evaluation",
        # Larger budget: batch of ~10-20 dissents × 1200 chars each
        total_budget=12_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="BatchReflectionOutput",
        task_data_builder=_build_batch_reflection_task_data,
    )


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_reflection_requested(event: dict, dispatcher) -> Optional[dict]:
    """
    REFLECTION_LOOP=v2 → batch mode: gather the last `REFLECTION_BATCH_HOURS`
    of dissenting runs, propose lessons that recur across them.

    Default (legacy) → original per-event behaviour: read one run, judge if
    it generalizes.

    Batch mode is the higher-leverage redesign because lessons that only
    show up in a single run are usually one-off mistakes; lessons that
    show up in three are patterns worth recording.
    """
    impl = os.environ.get("REFLECTION_LOOP", "v2").lower()
    if impl == "v2":
        return await _handle_batch_reflection(event, dispatcher)
    return await _handle_legacy_reflection(event, dispatcher)


async def _handle_legacy_reflection(event: dict, dispatcher) -> Optional[dict]:
    target_run_id = event["target_id"]

    async with dispatcher.pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT invocation_type, output_summary, status "
            "FROM agent_runs WHERE id = $1",
            target_run_id,
        )
    if run is None:
        return {"skipped": True, "reason": "run not found"}

    prompt = await dispatcher.curator.build(
        invocation_type="reflect.lesson_propose",
        context={
            "invocation_type": run["invocation_type"],
            "run_summary":     run["output_summary"] or "(no summary)",
        },
    )

    reflection, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ReflectionOutput,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="reflect_legacy",
    )

    if not reflection.should_create_lesson or reflection.candidate is None:
        return {
            "lesson_created": False,
            "reasoning": reflection.reasoning,
            "run_id": run_id,
        }

    lesson_id = await _persist_or_credit_lesson(
        dispatcher, reflection.candidate, target_run_id,
    )

    return {
        "lesson_created": lesson_id is not None,
        "lesson_id":      lesson_id,
        "lesson_text":    reflection.candidate.lesson_text,
        "run_id":         run_id,
    }


# v2 batch handler -----------------------------------------------------------

# How far back to gather dissenting runs. Default 7 days — short enough that
# the LLM batch fits comfortably, long enough that recurring patterns have
# room to repeat. Override via env.
_BATCH_HOURS = int(os.environ.get("REFLECTION_BATCH_HOURS", "168"))

# Cap on how many runs go into one batch prompt. Per-run summaries are capped
# in the builder (1200 chars), so 20 runs ≈ 24KB worst case — well inside the
# 12k token budget once the system + recall layers are added.
_BATCH_MAX_RUNS = 20

# Dedup: don't fire batch reflection more than once per N seconds. The trigger
# fires per event, but batch mode wants weekly cadence. We approximate that
# by short-circuiting when a recent batch already ran successfully.
_BATCH_MIN_GAP_SECONDS = 6 * 3600


async def _handle_batch_reflection(event: dict, dispatcher) -> Optional[dict]:
    async with dispatcher.pool.acquire() as conn:
        # Dedup: did a successful batch reflection run recently?
        recent_batch = await conn.fetchval(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - started_at))::int
            FROM agent_runs
            WHERE invocation_type = 'reflect.batch_propose_lessons'
              AND status = 'completed'
            ORDER BY started_at DESC LIMIT 1
            """
        )
        if recent_batch is not None and recent_batch < _BATCH_MIN_GAP_SECONDS:
            return {
                "skipped": True,
                "reason": f"recent batch reflection {recent_batch}s ago (< {_BATCH_MIN_GAP_SECONDS}s)",
            }

        rows = await conn.fetch(
            f"""
            SELECT id, invocation_type, output_summary, started_at
            FROM agent_runs
            WHERE status = 'completed'
              AND output_summary IS NOT NULL
              AND started_at > NOW() - INTERVAL '{_BATCH_HOURS} hours'
              -- focus on invocation types where dissent is informative
              AND invocation_type IN (
                  'critic.kill_verdict',
                  'adversary.judge_verdict',
                  'evaluation.slop_score',
                  'evaluation.batch_score',
                  'pi.claim_verdict'
              )
            ORDER BY started_at DESC LIMIT $1
            """,
            _BATCH_MAX_RUNS,
        )

    if not rows:
        return {"skipped": True, "reason": f"no dissenting runs in last {_BATCH_HOURS}h"}

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    def _ago(ts) -> str:
        delta = (now - ts).total_seconds()
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"

    runs = [
        {"id": r["id"], "invocation_type": r["invocation_type"],
         "output_summary": r["output_summary"], "ago": _ago(r["started_at"])}
        for r in rows
    ]

    prompt = await dispatcher.curator.build(
        invocation_type="reflect.batch_propose_lessons",
        context={"runs": runs},
    )

    output, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=BatchReflectionOutput,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="batch_propose_lessons",
    )

    created_ids: list[int] = []
    for cand in output.lessons:
        lid = await _persist_or_credit_lesson(dispatcher, cand, run_id)
        if lid is not None:
            created_ids.append(lid)

    return {
        "batch_size":    len(runs),
        "lessons":       len(created_ids),
        "lesson_ids":    created_ids,
        "run_id":        run_id,
        "reasoning":     output.reasoning,
    }


# =========================================================================
# Hinge A — judge applied lessons (the missing joint in the learning loop)
#
# The router records every lesson it injected into a prompt as a
# lesson_applications row with outcome NULL (router.py). Nothing ever judged
# those outcomes, so reconcile_lessons() (which needs them) could never fire.
# This closes that joint: per completed run, judge each applied lesson against
# what the run actually produced.
#
# Wired into the watchdog behind LESSON_JUDGE=on (default off) so it ships
# inert and is enabled only after shadow validation — same discipline as the
# *_LOOP=v2 gates.
# =========================================================================

from typing import Literal


class LessonJudgement(BaseModel):
    lesson_id: int
    verdict: Literal["supportive", "contradicting", "inconclusive"] = Field(
        ..., description="Did this lesson help (supportive), hurt/contradict (contradicting), or neither (inconclusive)?"
    )


class ApplicationJudgements(BaseModel):
    judgements: list[LessonJudgement] = Field(default_factory=list)


async def _build_judge_applications_task_data(ctx: dict, state, memory) -> PromptLayer:
    lessons = ctx["lessons"]  # list[{id, text}]
    lesson_block = "\n".join(f"- [{l['id']}] {l['text']}" for l in lessons)
    content = f"""## Judge whether applied lessons helped this run

A past run was given the lessons below as guidance, then produced the outcome
shown. For EACH lesson, judge whether it was:

  - `supportive`     — the run followed it and it clearly helped the outcome
  - `contradicting`  — the run's outcome shows the lesson was wrong or harmful here
  - `inconclusive`   — the lesson neither helped nor hurt this particular run

**Default to `inconclusive`.** Most lessons are merely benign on any given run;
only mark supportive/contradicting when the outcome gives real evidence either way.

## Run
**Invocation:** {ctx['invocation_type']}
**Status:** {ctx['run_status']}
**Expectation (if recorded):** {ctx.get('expectation') or '(none)'}
**Outcome / output:** {(ctx.get('outcome') or ctx.get('output_summary') or '(no summary)')[:1500]}

## Lessons that were applied
{lesson_block}

Return one judgement per lesson id above.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "reflect.judge_applications" not in RECIPES:
    RECIPES["reflect.judge_applications"] = Recipe(
        invocation_type="reflect.judge_applications",
        description="Judge whether each lesson applied to a run helped, hurt, or was neutral.",
        agent="evaluation",
        total_budget=6_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ApplicationJudgements",
        task_data_builder=_build_judge_applications_task_data,
    )


async def judge_pending_lesson_applications(dispatcher, limit: int = 40) -> dict:
    """
    Hinge A driver. Fetch unjudged lesson applications on completed runs, judge
    them per run, and write the outcomes. Returns a small summary.

    Safe to call repeatedly; `set_application_outcome` only fills NULL rows.
    Called from the watchdog when LESSON_JUDGE=on.
    """
    lessons = getattr(dispatcher, "lessons", None)
    curator = getattr(dispatcher, "curator", None)
    router = getattr(dispatcher, "router", None)
    if not (lessons and curator and router):
        return {"judged": 0, "skipped": "clients unavailable"}

    rows = await lessons.fetch_pending_applications(limit)
    if not rows:
        return {"judged": 0}

    by_run: dict[int, list[dict]] = {}
    for r in rows:
        by_run.setdefault(r["agent_run_id"], []).append(r)

    judged = 0
    for run_id, apps in by_run.items():
        applied_ids = {a["lesson_id"] for a in apps}
        run_success = apps[0]["run_status"] == "completed"
        ctx = {
            "invocation_type": apps[0]["invocation_type"],
            "run_status": apps[0]["run_status"],
            "expectation": apps[0].get("expectation"),
            "outcome": apps[0].get("outcome"),
            "output_summary": apps[0].get("output_summary"),
            "lessons": [{"id": a["lesson_id"], "text": a["lesson_text"]} for a in apps],
        }
        try:
            prompt = await curator.build(invocation_type="reflect.judge_applications", context=ctx)
            out, judge_run_id = await router.invoke(
                prompt=prompt, output_schema_class=ApplicationJudgements,
            )
        except Exception:
            log.exception("judge_applications failed for run %s — skipping", run_id)
            continue
        for j in out.judgements:
            if j.lesson_id not in applied_ids:
                continue  # server-side guard: only judge lessons actually applied
            verdict = j.verdict
            # Only credit 'supportive' when the run actually succeeded.
            if verdict == "supportive" and not run_success:
                verdict = "inconclusive"
            await lessons.set_application_outcome(
                lesson_id=j.lesson_id, agent_run_id=run_id,
                outcome=verdict, judged_by_run_id=judge_run_id,
            )
            judged += 1
    log.info("judge_applications: judged %d lesson applications across %d runs", judged, len(by_run))
    return {"judged": judged, "runs": len(by_run)}
