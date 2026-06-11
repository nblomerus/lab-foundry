"""
The synthesis agent's handler — the terminal step.

`finding.synthesize` (emitted by the experiments lane once a direction has enough
completed runs) → read the direction + ALL its completed experiments → compose a
paper-shaped ResearchFinding (one LLM step, `synthesis.compose`) → persist it as a
`finding` claim + a research_findings row, graduate the direction, and ingest the
finding into the Library (source_kind='lab_finding') so it compounds and feeds
Ariadne's next deliberation. The mode-dial agent name is `synthesis`.
"""

from __future__ import annotations

import json
import logging
import os

from agents.synthesis.schemas import ResearchFinding
from harness.curator import RECIPES, SYSTEM_PROMPTS, PromptLayer, Recipe
from harness.router import ROUTE, Tier

log = logging.getLogger(__name__)

# Condition-driven trigger thresholds (read once at import; tune via env). A direction is
# ready to synthesize at MIN completed experiments, and re-synthesizes only when STEP more
# have accumulated — so a finding rests on materially more evidence each time, not on every run.
SYNTHESIS_MIN_EXPERIMENTS = int(os.environ.get("SYNTHESIS_MIN_EXPERIMENTS", "3"))
SYNTHESIS_RESYNTH_STEP = int(os.environ.get("SYNTHESIS_RESYNTH_STEP", str(SYNTHESIS_MIN_EXPERIMENTS)))
# A finding this confident AND decisive (supported / refuted / mixed — NOT inconclusive) CONCLUDES
# its direction: a permanent result that frees the gate. Below this, the direction stays open.
SYNTHESIS_CONCLUDE_CONFIDENCE = float(os.environ.get("SYNTHESIS_CONCLUDE_CONFIDENCE", "0.6"))


# -------------------------------------------------------------------------
# Curator task_data builder
# -------------------------------------------------------------------------


def _format_experiments(experiments: list[dict]) -> str:
    """Render the direction's completed experiments compactly for the compose prompt —
    each one's hypothesis, the numbers it produced, and the researcher's read of it."""
    blocks = []
    for e in experiments:
        params = e.get("params") or {}
        result = e.get("result") or {}
        note = e.get("researcher_notes") or e.get("interpretation") or ""
        blocks.append(
            f"### Experiment {e.get('experiment_id') or e.get('id')}\n"
            f"**Hypothesis:** {params.get('hypothesis') or '(none recorded)'}\n"
            f"**Result:** {json.dumps(result)[:1400]}\n"
            f"**Read:** {note[:700]}\n"
        )
    return "\n".join(blocks) or "(no completed experiments)"


async def _build_compose(ctx: dict, state, memory) -> PromptLayer:
    direction = ctx.get("direction_statement") or "(no direction statement)"
    goals = ctx.get("goals") or ""
    experiments = ctx.get("experiments") or []

    content = f"""## Direction
{direction}

## What the direction set out to show (goals / kill-conditions)
{goals or "(none recorded)"}

## Completed experiments on this direction (the evidence)
{_format_experiments(experiments)}

---

You are writing the lab's TERMINAL result for this direction — the paper-shaped finding the
accumulated experiments support. Read ACROSS all the experiments above (do not re-summarize one).

Produce a ResearchFinding:
- `headline`: one paper-title sentence stating the result with a quantified effect.
- `claim`: the single defensible claim the numbers support — stated so it could be cited.
- `supported`: supported / refuted / mixed / inconclusive — be honest. A REFUTED direction is a
  real, publishable finding (the literature's claim did not transfer); say so plainly. If the
  experiments don't actually settle the question, say `inconclusive` — do NOT manufacture a result.
- `method`: how it was tested across the runs (datasets, models, metrics, controls).
- `key_numbers`: the ACTUAL numbers that carry the claim — effects, deltas, costs. Cite them; no vague "improved".
- `limitations`: honest scope — toy/synthetic data, single GPU, small N, confounds, what was NOT shown.
- `so_what`: the concrete decision a named practitioner changes because of this. If you can't name one,
  the finding is weak — lower `confidence` accordingly.
- `confidence`: 0..1, calibrated DOWN for toy/synthetic/small-N evidence. A handful of seeded toy runs is
  suggestive, not decisive.
- `grounded_in_experiments`: the experiment ids you actually used.

Do not invent evidence not present above. Do not propose a new hypothesis — conclude.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe + route + system-prompt registration (idempotent — guard double-import)
# -------------------------------------------------------------------------

SYSTEM_PROMPTS.setdefault(
    "synthesis",
    (
        "You are a research scientist in an autonomous AI lab writing up results. You read a direction's "
        "completed experiments and compose the single defensible, paper-shaped finding they support — "
        "claim, method, numbers, limitations, and who acts on it. You are ruthlessly honest: a refuted "
        "hypothesis or an inconclusive body of evidence is the finding; you never inflate toy results into "
        "conclusions, and you always state what was NOT shown."
    ),
)

if "synthesis.compose" not in RECIPES:
    RECIPES["synthesis.compose"] = Recipe(
        invocation_type="synthesis.compose",
        description="Compose a direction's completed experiments into one paper-shaped research finding.",
        agent="synthesis",
        total_budget=10_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ResearchFinding",
        task_data_builder=_build_compose,
    )

ROUTE.setdefault("synthesis.compose", Tier.WORKHORSE)


def _graduate_to(supported: str, confidence: float) -> str:
    """Map a finding to the direction's new lifecycle status. A confident, DECISIVE finding
    (supported / refuted / mixed — a real result either way) CONCLUDES the direction: terminal,
    a permanent result that leaves the active set and frees the gate. A weaker or inconclusive
    finding keeps it open (more experiments might settle it). Internal experiments never earn
    'replicated' (that implies independent replication) — the ceiling is honest."""
    if supported != "inconclusive" and confidence >= SYNTHESIS_CONCLUDE_CONFIDENCE:
        return "concluded"
    if supported == "supported":
        return "weakly_supported"
    return "tested"


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------


async def handle_finding_synthesize(event: dict, dispatcher) -> dict | None:
    """`finding.synthesize` → compose the direction's experiments into a finding, persist it,
    graduate the direction, and ingest the finding into the Library."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    claim_id = payload.get("claim_id")
    if claim_id is None:
        return {"skipped": True, "reason": "no claim_id in finding.synthesize payload"}

    try:
        direction = await state.get_claim(claim_id)
    except ValueError:
        return {"skipped": True, "reason": f"direction {claim_id} not found/active", "claim_id": claim_id}

    experiments = await state.get_completed_experiments_for_claim(claim_id)
    n = len(experiments)
    if n < SYNTHESIS_MIN_EXPERIMENTS:
        return {
            "skipped": True,
            "reason": f"only {n} completed experiments (< {SYNTHESIS_MIN_EXPERIMENTS})",
            "claim_id": claim_id,
        }

    # Idempotency: don't re-synthesize the same evidence. Skip if a finding already rests on
    # at least this many experiments (the event's bucketed dedup makes this rare, but races happen).
    last_n = await state.latest_finding_n_for_claim(claim_id)
    if last_n is not None and last_n >= n:
        return {"skipped": True, "reason": f"already synthesized at n={last_n} >= {n}", "claim_id": claim_id}

    goals = await state.get_claim_goals_text(claim_id)
    prompt = await dispatcher.curator.build(
        invocation_type="synthesis.compose",
        context={"direction_statement": direction.statement, "goals": goals, "experiments": experiments},
    )
    finding, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ResearchFinding,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="synthesis.compose",
    )

    # The experiment ids this finding actually rests on (constrained to the real, completed set).
    valid_ids = {int(e.get("experiment_id") or e.get("id")) for e in experiments}
    used = [i for i in (finding.grounded_in_experiments or []) if i in valid_ids] or sorted(valid_ids)

    graduate_to = _graduate_to(finding.supported, finding.confidence)
    persisted = await state.persist_research_finding(
        direction_claim_id=claim_id,
        headline=finding.headline,
        claim_text=finding.claim,
        supported=finding.supported,
        method=finding.method,
        key_numbers=finding.key_numbers,
        limitations=finding.limitations,
        so_what=finding.so_what,
        next_step=finding.next_step,
        confidence=finding.confidence,
        n_experiments=n,
        grounded_in=[f"exp:{i}" for i in used],
        graduate_to=graduate_to,
        run_id=run_id,
    )

    # Ingest the finding into the Library so it becomes first-class, queryable knowledge that
    # SURVIVES Ariadne's next re-frame (unlike per-experiment notes bonded to the direction id).
    canonical_key = f"finding:{claim_id}:{n}"
    await state.emit_corpus_event(
        "source.discovered",
        target_type="source",
        target_id=claim_id,
        payload={
            "source": {
                "kind": "note",
                "source_kind": "lab_finding",
                "canonical_key": canonical_key,
                "title": finding.headline[:200],
                "why": "first-party lab research finding",
            },
            "content": _finding_markdown(claim_id, direction.statement, finding, used),
            "provenance": {
                "direction_id": claim_id,
                "n_experiments": n,
                "grounded_in_experiments": [f"exp:{i}" for i in used],
                "supported": finding.supported,
                "confidence": finding.confidence,
            },
        },
        dedup_key=f"finding-doc-{claim_id}-{n}",
    )

    log.info(
        "synthesis: composed finding for direction %s (supported=%s conf=%.2f, %d experiments → %s)",
        claim_id,
        finding.supported,
        finding.confidence,
        n,
        graduate_to,
    )
    return {
        "claim_id": claim_id,
        "finding_id": persisted.get("finding_id"),
        "finding_claim_id": persisted.get("finding_claim_id"),
        "supported": finding.supported,
        "confidence": finding.confidence,
        "n_experiments": n,
        "graduated_to": persisted.get("graduated_to"),
        "compose_run_id": run_id,
    }


def _finding_markdown(direction_id, direction_statement: str, finding: ResearchFinding, used: list[int]) -> str:
    cited = ", ".join(f"exp:{i}" for i in used) or "(none)"
    return (
        f"# Finding: {finding.headline}\n\n"
        f"**Direction (id {direction_id}):** {direction_statement}\n\n"
        f"**Claim.** {finding.claim}  \n"
        f"_Verdict: {finding.supported} · confidence {finding.confidence:.2f}_\n\n"
        f"**Method.** {finding.method}\n\n"
        f"**Results.** {finding.key_numbers}\n\n"
        f"**Limitations.** {finding.limitations}\n\n"
        f"**So what.** {finding.so_what}\n\n"
        f"**Next step.** {finding.next_step or '(none)'}\n\n"
        f"---\n"
        f"Synthesized from {len(used)} first-party lab experiment(s): {cited}.\n"
    )
