"""
Handler for task.completed events.

Flow:
  1. Read the completed task + its unaudited findings.
  2. Invoke the Evaluation (FAST tier) to score every finding for slop.
  3. Persist verdicts (update_finding_audit emits finding.high_signal as needed).
  4. Check the slop circuit-breaker per affected claim.
  5. Write an entry to the Zep 'dissent' session if any slop was found.

This handler is also the canonical example for the rest: it touches every
piece of the harness — state, curator, router, memory, events.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

from harness.curator import (
    RECIPES,
    PromptLayer,
    Recipe,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Evaluation output schema
# -------------------------------------------------------------------------


class AuditScore(BaseModel):
    finding_id: int
    audit_score: float = Field(..., ge=0.0, le=1.0, description="0 = slop, 1 = high-quality research.")
    verdict: Literal["pass", "slop", "unclear"]
    reasoning: str = Field(..., description="1-2 sentences justifying the verdict.")


class AuditBatch(BaseModel):
    scores: list[AuditScore]


def _verdict_from_score(score: float) -> str:
    """Derive the verdict deterministically from the (well-calibrated) audit_score
    rather than trusting the model's separately-emitted `verdict` field, which is
    noisy on weak evaluations (~18% mislabel — e.g. a 0.85 score tagged "slop"),
    inflating the slop rate and tripping the circuit-breaker on healthy claims.
    Rubric: slop 0.0-0.3 · unclear 0.3-0.7 · pass 0.7-1.0."""
    if score < 0.3:
        return "slop"
    if score < 0.7:
        return "unclear"
    return "pass"


# -------------------------------------------------------------------------
# Evaluation recipe (registered at import time)
# -------------------------------------------------------------------------


async def _build_evaluation_task_data(ctx: dict, state, memory) -> PromptLayer:
    findings = ctx["findings"]
    task = ctx["task"]
    # Evidence is the per-page quote/claim trail the researcher built up before
    # synthesizing findings. Passing it to the evaluation lets it judge whether
    # each finding is grounded in what the source pages actually said, not
    # just whether the finding *sounds* substantive in isolation.
    evidence: list[dict] = ctx.get("evidence") or []
    # Experiments contribute concrete numbers (counts, ratios, star counts) that
    # findings may legitimately cite even without a quote-row backing them.
    # Without this section the evaluation flags experiment-derived findings as
    # ungrounded (a false negative).
    experiments: list[dict] = ctx.get("experiments") or []

    finding_blocks = []
    for f in findings:
        finding_blocks.append(
            f"### Finding F{f.id}\n"
            f"- Source: {f.source or 'n/a'}\n"
            f"- URL: {f.url or 'n/a'}\n"
            f"- Title: {f.title or 'n/a'}\n"
            f"- Summary: {f.summary}\n"
            f"- Researcher relevance score: {f.relevance_score}\n"
            f"- Why it matters: {f.why_it_matters or '(none)'}\n"
            f"- Supports claim: {f.supports_thesis}"
        )

    # Group evidence by URL so the evaluation can see which pages backed which
    # claims at a glance. Cap each page to its top 3 evidence items and the
    # whole block to 40 lines so the prompt stays bounded.
    by_url: dict[str, list[dict]] = {}
    for e in evidence:
        by_url.setdefault(e["url"], []).append(e)

    evidence_blocks: list[str] = []
    for url, items in list(by_url.items())[:20]:
        lines = [f"### Page: {url}"]
        for e in items[:3]:
            stance = e["stance"]
            conf = float(e["confidence"])
            lines.append(
                f"- [SQ{e['sub_question_idx']} · {stance} · conf {conf:.2f}] "
                f"{e['claim']}\n"
                f'  quote: "{(e["quote"] or "")[:240]}"'
            )
        evidence_blocks.append("\n".join(lines))

    evidence_section = (
        "\n".join(evidence_blocks)
        if evidence_blocks
        else "(no evidence trail — likely a legacy single-shot researcher run)"
    )

    # Experiment block — kind, params, key result fields, interpretation. We
    # don't dump full results (can be 5 KB+); the interpretation already
    # captures the meaningful numbers.
    exp_blocks: list[str] = []
    for x in experiments[:10]:
        if x.get("status") != "completed":
            continue
        params_str = json.dumps(x.get("params") or {}, ensure_ascii=False)[:300]
        interp = (x.get("interpretation") or "")[:600]
        exp_blocks.append(f"### Experiment X{x['id']} ({x['kind']})\n- Params: {params_str}\n- Interpretation: {interp}")
    experiment_section = (
        "\n".join(exp_blocks) if exp_blocks else "(no experiments — findings must ground in evidence quotes alone)"
    )

    content = f"""## Task being audited

**Task:** {task.description}
**Type:** {task.task_type}

## Evidence the researcher saw ({len(evidence)} items across {len(by_url)} pages)

{evidence_section}

## Experiments the researcher ran ({len([x for x in experiments if x.get("status") == "completed"])} completed)

{experiment_section}

## Findings produced ({len(findings)})

{chr(10).join(finding_blocks) if finding_blocks else "(no findings)"}

---

For each finding, score it 0-1 against two criteria together (use the
**lower** of the two when picking the score):

  1. **Substance.** Could this finding have been written without doing the
     research? Generic explainers, "X is a powerful tool", vague summaries
     of categories → slop.

  2. **Groundedness.** Does the finding's claim actually match what the
     researcher saw? A finding is grounded when its claim is supported by:
       (a) an evidence quote above, OR
       (b) a completed experiment's interpretation/numbers above.
     A finding that cites star counts or growth ratios from a completed
     `compare_repo_growth` / `gh_search_trend` experiment IS grounded —
     the experiment IS the source. A finding that makes a claim with no
     backing in either section is ungrounded.

Also flag obvious **duplicates** — two findings making the same claim from
the same source should both be scored at most `unclear`; one of them isn't
adding information. Don't pass duplicates.

Verdict bands:
  - `pass`    (0.7-1.0): substantive AND grounded — the finding's claim is
                          directly supported by ≥1 quote above.
  - `slop`    (0.0-0.3): generic OR ungrounded — the finding overreaches
                          the evidence, or could have been written without
                          reading the cited material.
  - `unclear` (0.3-0.7): mixed — partially grounded, or grounded but vague.

Be ruthless. Pass is reserved for findings that are both load-bearing and
faithfully tied to the evidence trail. Most findings should land in pass
or slop; unclear is a fallback, not a default.

Return one entry per finding. Use the same finding_id you saw.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "evaluation.slop_score" not in RECIPES:
    RECIPES["evaluation.slop_score"] = Recipe(
        invocation_type="evaluation.slop_score",
        description="Evaluation scores findings for substance AND groundedness against the evidence trail.",
        agent="evaluation",
        # Bumped from 8k to 14k after the evaluation was given the per-evidence
        # trail (quotes + claims grouped by URL) so it can score groundedness
        # in addition to substance. The evidence section is the largest growth
        # — up to ~20 pages × 3 items × ~300 chars ≈ 4500 tokens — and the
        # curator's budget enforcer drops priority>=2 layers (lessons, recall)
        # first if we still overflow, which is the right tradeoff.
        total_budget=14_000,
        use_cold_path=False,
        recall_sessions=["dissent"],  # see recent dissent for calibration
        recall_k=5,
        output_schema="AuditBatch",
        task_data_builder=_build_evaluation_task_data,
    )


# -------------------------------------------------------------------------
# The handler
# -------------------------------------------------------------------------


async def handle_task_completed(event: dict, dispatcher) -> dict | None:
    """
    Audit the findings of one completed task. Emits high-signal and slop events
    downstream via the state client.

    Required on `dispatcher`:  state, memory, curator, router
    """
    task_id = event["target_id"]
    task = await dispatcher.state.get_task(task_id)

    if task.department != "research":
        return {"skipped": True, "reason": "non-research task"}

    findings = await dispatcher.state.get_unaudited_findings_for_task(task_id)
    if not findings:
        return {"skipped": True, "reason": "no unaudited findings"}

    # In-memory lookup so the graph-sink below can resolve a scored finding
    # without re-fetching it from the DB (falls back to get_finding if missing).
    by_id_for_graph = {f.id: f for f in findings}

    # Pull the evidence trail AND the experiment runs too, so the evaluation can
    # score groundedness against both. For legacy single-shot researcher tasks
    # both lists return empty.
    evidence = await dispatcher.state.get_evidence_for_task(task_id)
    experiments = await dispatcher.state.get_experiment_runs_for_task(task_id)

    # AUDITOR_LOOP=v2 → per-finding cross_check + batch_score (agents.evaluation.loop).
    # Legacy single-call path is the default until the loop is validated by
    # shadow runs and bench comparison.
    impl = os.environ.get("AUDITOR_LOOP", "v2").lower()
    if impl == "v2":
        from agents.evaluation.loop import run_audit_loop

        audit_batch_v2, run_id = await run_audit_loop(
            task=task,
            findings=findings,
            evidence=evidence,
            experiments=experiments,
            dispatcher=dispatcher,
            triggered_by_event_id=event["id"],
        )
        # The v2 schema is structurally identical to the legacy one; we re-cast
        # so the rest of the handler operates on a consistent type. (Both
        # modules define AuditBatch in their own namespace.)
        audit_batch = AuditBatch(scores=[s.model_dump() for s in audit_batch_v2.scores])
        if run_id == 0:
            # All cross-checks failed — skip the rest of the handler. The
            # event will be marked consumed but findings remain unaudited;
            # the watchdog re-emits task.completed if nothing audits them.
            return {"skipped": True, "reason": "v2 audit: all cross_check steps failed"}
    else:
        prompt = await dispatcher.curator.build(
            invocation_type="evaluation.slop_score",
            context={
                "task": task,
                "findings": findings,
                "evidence": evidence,
                "experiments": experiments,
                "task_id": task_id,
            },
        )
        audit_batch, run_id = await dispatcher.router.invoke(
            prompt=prompt,
            output_schema_class=AuditBatch,
            triggered_by_event_id=event["id"],
        )

    # Persist each verdict; high-signal events emitted inside update_finding_audit.
    # Verdict is derived from the score (not the model's noisy verdict field).
    derived = [(s, _verdict_from_score(s.audit_score)) for s in audit_batch.scores]
    for score, verdict in derived:
        await dispatcher.state.update_finding_audit(
            finding_id=score.finding_id,
            audit_score=score.audit_score,
            audit_verdict=verdict,
            run_id=run_id,
        )

        # Write high-signal findings to graph (non-fatal if Neo4j unavailable)
        if verdict == "pass" and score.relevance_score >= 8:
            try:
                from library.graph.tools import merge_finding_grounds_claim

                finding = by_id_for_graph.get(score.finding_id) if "by_id_for_graph" in locals() else None
                if finding is None:
                    finding = await dispatcher.state.get_finding(score.finding_id)
                if finding and finding.claim_id:
                    claim = await dispatcher.state.get_claim(finding.claim_id)
                    await merge_finding_grounds_claim(
                        finding_id=finding.id,
                        claim_id=finding.claim_id,
                        source=finding.source or "",
                        url=finding.url,
                        title=finding.title,
                        summary=finding.summary,
                        relevance_score=finding.relevance_score,
                        supports_claim=finding.supports_thesis,
                        audit_verdict=verdict,
                        created_at=finding.created_at,
                    )
            except Exception:
                log.exception("graph_sink: finding→claim write failed — continuing")

    # Slop circuit-breaker per affected claim
    claims_seen = {f.claim_id for f in findings if f.claim_id is not None}
    breakers_tripped: list[int] = []
    for claim_id in claims_seen:
        if await dispatcher.state.detect_slop_breaker(claim_id):
            breakers_tripped.append(claim_id)

    # Upward force on confidence. The critic and slop-breaker only push
    # confidence DOWN — without a counterweight every claim decays to 0 and
    # nothing can ever be validated. A pass + high-relevance + supporting
    # finding is genuine positive signal, so reinforce its claim.
    by_id = {f.id: f for f in findings}
    support: dict[int, int] = {}
    for s, verdict in derived:
        f = by_id.get(s.finding_id)
        if (
            f is not None
            and verdict == "pass"
            and f.relevance_score >= 8
            and f.supports_thesis is True
            and f.claim_id is not None
        ):
            support[f.claim_id] = support.get(f.claim_id, 0) + 1
    for claim_id, n in support.items():
        claim = await dispatcher.state.get_claim(claim_id)
        new_conf = min(1.0, claim.confidence + min(0.20, 0.08 * n))
        if new_conf > claim.confidence:
            await dispatcher.state.update_claim_confidence(
                claim_id=claim_id,
                new_confidence=new_conf,
                reason=f"reinforced by {n} validated supporting finding(s) (pass, rel>=8)",
                run_id=run_id,
            )

    # Narrate the audit into dissent session if anything was flagged
    slop_count = sum(1 for _, v in derived if v == "slop")
    if slop_count > 0:
        await dispatcher.memory.write_message(
            session_id="dissent",
            content=(
                f"Evaluation reviewed task T{task_id} "
                f"('{task.description[:80]}...' if longer than 80 else task.description). "
                f"{slop_count} of {len(audit_batch.scores)} findings flagged as slop. "
                f"Theses affected: {sorted(claims_seen)}. "
                f"Circuit-breakers tripped: {breakers_tripped or 'none'}."
            ),
            role_type="evaluation",
            metadata={"task_id": task_id, "run_id": run_id},
        )

    return {
        "audited": len(audit_batch.scores),
        "slop": slop_count,
        "pass": sum(1 for s in audit_batch.scores if s.verdict == "pass"),
        "unclear": sum(1 for s in audit_batch.scores if s.verdict == "unclear"),
        "breakers_tripped": breakers_tripped,
        "run_id": run_id,
    }
