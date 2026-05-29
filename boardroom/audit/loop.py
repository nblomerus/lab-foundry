"""
Auditor loop — two-step audit replacing the single-shot `auditor.slop_score`.

Pipeline per task.completed event:

    for each finding f (in parallel):
        cross_check_evidence_for_finding(f, full_evidence_trail) →
            ClaimCheck list + substance + duplicate flag

    batch_score(all cross_check reports + original findings) →
        AuditBatch (one final pass/slop/unclear score per finding)

Why two steps instead of one:

The legacy single-call auditor has to share its context budget across N
findings, so per-finding evidence is truncated to ~3 items × 240 chars.
Per-finding cross_check sees the full evidence relevant to ONE finding
without that cap — groundedness judgments stop being constrained by
aggregate prompt size. Final scoring is then a pure aggregation step that
operates over compact structured reports, not raw evidence trails.

The output schema for the final step is the same `AuditBatch` the legacy
path returns, so the rest of the handler (state writes, slop breaker,
confidence reinforcement, dissent narrative) is unchanged.

Behind `AUDITOR_LOOP=v2`. Legacy single-call path stays as the default
until the loop is validated against a week of shadow runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from boardroom.audit.schemas import (
    AuditBatch, ClaimCheck, EvidenceCrossCheck,
)
from boardroom.harness.curator import (
    PromptLayer, Recipe, RECIPES,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Prompt builders
# -------------------------------------------------------------------------

async def _build_cross_check_finding(ctx: dict, state, memory) -> PromptLayer:
    finding = ctx["finding"]
    task = ctx["task"]
    # Evidence already filtered to items relevant to this finding's task; the
    # cross-check sees ALL items, not a per-page sample. That's the point.
    evidence = ctx.get("evidence") or []
    experiments = ctx.get("experiments") or []

    ev_blocks: list[str] = []
    by_url: dict[str, list[dict]] = {}
    for e in evidence:
        by_url.setdefault(e["url"], []).append(e)
    for url, items in by_url.items():
        lines = [f"### Page: {url}"]
        for e in items:
            stance = e["stance"]
            conf = float(e["confidence"])
            lines.append(
                f"- [SQ{e['sub_question_idx']} · {stance} · conf {conf:.2f}] "
                f"{e['claim']}\n"
                f"  quote: \"{e['quote']}\""  # full quote, no truncation
            )
        ev_blocks.append("\n".join(lines))
    evidence_section = "\n".join(ev_blocks) or "(no evidence trail)"

    exp_blocks: list[str] = []
    for x in experiments:
        if x.get("status") != "completed":
            continue
        params_str = json.dumps(x.get("params") or {}, ensure_ascii=False)[:400]
        interp = x.get("interpretation") or ""
        exp_blocks.append(
            f"### Experiment X{x['id']} ({x['kind']})\n"
            f"- Params: {params_str}\n"
            f"- Interpretation: {interp}"
        )
    experiment_section = "\n".join(exp_blocks) or "(no completed experiments)"

    content = f"""## Audit one finding against its evidence trail

**Task:** {task.description}

## The finding under audit

**F{finding.id}** (researcher relevance {finding.relevance_score}, supports_thesis={finding.supports_thesis})

- Source: {finding.source or 'n/a'}
- URL: {finding.url or 'n/a'}
- Title: {finding.title or 'n/a'}
- Summary: {finding.summary}
- Why it matters: {finding.why_it_matters or '(none)'}

## Evidence the researcher saw ({len(evidence)} items across {len(by_url)} pages)

{evidence_section}

## Experiments the researcher ran ({len([x for x in experiments if x.get('status') == 'completed'])} completed)

{experiment_section}

---

Decompose the finding into 3-6 specific **claims** (facts, comparisons,
predictions, recommendations) and check each against the evidence above.
For each claim:

- `claim`: rephrase in your words, one short sentence.
- `quote`: the exact verbatim quote from the evidence (or experiment
  interpretation) that backs the claim. Null if no evidence backs it.
- `source_url`: the URL or experiment id where the quote lives. Null if no
  quote.
- `match`:
    * `yes` — the quote/experiment directly supports the claim
    * `partial` — the quote is related but the finding *overreaches*
                   (claim is stronger than the quote licenses)
    * `no` — no quote in the evidence backs this claim (likely fabricated
              or pattern-matched from prior knowledge)

Then judge the finding overall:

- `substance`: low (generic, could have been written without the research)
               / medium (somewhat specific, vague in places)
               / high (concrete numbers, named entities, specific events).
- `duplicate_of_finding_id`: if this finding restates an earlier one in
  the same audit batch, set its F-id. Otherwise null.
- `notes`: one sentence — what's the most load-bearing observation about
  this finding?

A finding with all `yes` claims and high substance is a `pass` candidate.
A finding with `no` matches anywhere, or `low` substance, is slop. Don't
emit a final verdict — that's the next stage's job; just produce the
structured trace.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_batch_score(ctx: dict, state, memory) -> PromptLayer:
    task = ctx["task"]
    findings = ctx["findings"]
    cross_checks = ctx["cross_checks"]  # list[dict] of EvidenceCrossCheck

    by_id = {f.id: f for f in findings}
    blocks: list[str] = []
    for c in cross_checks:
        f = by_id.get(c["finding_id"])
        if f is None:
            continue
        claim_lines = []
        for cl in c.get("claims", []):
            match = cl["match"]
            q = (cl.get("quote") or "")[:200]
            claim_lines.append(
                f"  - [{match}] {cl['claim']}"
                + (f"\n      quote: \"{q}\"" if q else "")
            )
        claims_block = "\n".join(claim_lines) or "  (no claims extracted)"
        dup = c.get("duplicate_of_finding_id")
        blocks.append(
            f"### F{f.id}  (substance: {c.get('substance', '?')}"
            + (f", DUPLICATE of F{dup}" if dup else "")
            + ")\n"
            f"Summary: {f.summary[:300]}\n"
            f"Cross-check claims:\n{claims_block}\n"
            f"Notes: {c.get('notes', '')}"
        )

    content = f"""## Final audit scoring

**Task:** {task.description}

Each finding has already been cross-checked against the evidence trail.
You see the structured trace for every claim (yes / partial / no match),
the overall substance level, and any duplicate flag.

## Cross-check reports

{chr(10).join(blocks) if blocks else '(no findings — emit an empty scores list)'}

---

For each finding, emit one `AuditScore` with a final verdict and audit_score:

**Calibration rubric**

- `pass` (0.7-1.0): substance ≥ medium AND all claims `yes` (or 1 `partial`
  that doesn't overreach the load-bearing point). The finding's case is
  actually backed by what the researcher saw.

- `slop` (0.0-0.3): ANY of:
    * substance = low
    * one or more `no` matches (claim has no quote backing)
    * 2+ `partial` matches with overreach
    * duplicate_of_finding_id is set AND the original was equally strong

- `unclear` (0.3-0.7): everything else — partially grounded, or grounded
  but vague. Includes duplicates that add new framing but no new evidence.

Be ruthless about `no` matches: a finding that includes ONE fabricated
claim is contaminated even if the rest is solid. Don't pass it.

`reasoning`: one sentence pointing at the load-bearing reason (which
specific claim, which match status, which substance issue).
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe registration (idempotent — guards against double-import)
# -------------------------------------------------------------------------

_AUDITOR_RECIPES: list[tuple[str, str, int, str, callable]] = [
    (
        "auditor.cross_check_finding",
        "Cross-check one finding's claims against its evidence trail.",
        # Per-finding evidence can be sizeable when a thesis has many cached
        # pages; 10k leaves room for the full quotes of ~30 items.
        10_000,
        "EvidenceCrossCheck",
        _build_cross_check_finding,
    ),
    (
        "auditor.batch_score",
        "Aggregate per-finding cross-check reports into final pass/slop scores.",
        # Cross-check reports are structured + compact. 8k is plenty for ~12
        # findings × ~600 chars/report.
        8_000,
        "AuditBatch",
        _build_batch_score,
    ),
]

for _itype, _desc, _budget, _schema, _builder in _AUDITOR_RECIPES:
    if _itype not in RECIPES:
        RECIPES[_itype] = Recipe(
            invocation_type=_itype,
            description=_desc,
            agent="auditor",
            total_budget=_budget,
            use_cold_path=False,
            # Both stages benefit from a quick glance at recent dissent for
            # calibration. Kept small so the budget goes mostly to the
            # evidence trail / cross-check reports.
            recall_sessions=["dissent"],
            recall_k=3,
            output_schema=_schema,
            task_data_builder=_builder,
        )


# -------------------------------------------------------------------------
# Orchestrator
# -------------------------------------------------------------------------

# Cap parallelism on cross_check. Each call is independent so they can fan
# out, but a 12-finding task at unbounded concurrency would hammer the
# router's per-model GPU lock and starve other handlers. 4 mirrors the
# dispatcher's max_concurrent_handlers default.
MAX_CONCURRENT_CROSS_CHECKS = 4


async def run_audit_loop(
    *,
    task,
    findings,
    evidence: list[dict],
    experiments: list[dict],
    dispatcher,
    triggered_by_event_id: Optional[int] = None,
) -> tuple[AuditBatch, int]:
    """
    Two-step audit. Returns (final_batch, anchor_run_id) where anchor_run_id
    is the batch_score step's id — used by the caller for state writes that
    need a run_id (update_finding_audit, update_thesis_confidence).
    """
    router = dispatcher.router
    curator = dispatcher.curator
    session = dispatcher.session  # None when called outside a handler context

    # ---- 1. cross_check per finding (parallel, bounded) -----------------
    sem = asyncio.Semaphore(MAX_CONCURRENT_CROSS_CHECKS)

    async def _check_one(f) -> Optional[EvidenceCrossCheck]:
        async with sem:
            prompt = await curator.build(
                invocation_type="auditor.cross_check_finding",
                context={
                    "task": task,
                    "finding": f,
                    "evidence": evidence,
                    "experiments": experiments,
                },
            )
            try:
                check, _run_id = await router.invoke(
                    prompt=prompt,
                    output_schema_class=EvidenceCrossCheck,
                    triggered_by_event_id=triggered_by_event_id,
                    session=session,
                    step_name=f"cross_check_finding/F{f.id}",
                    # Fan-out: all cross_checks are roots within this session
                    # so the DAG shows them as parallel siblings rather than
                    # a linear chain that doesn't reflect the actual flow.
                    parent_step_id=None,
                )
                return check
            except Exception as e:  # noqa: BLE001 — one finding's failure shouldn't kill the audit
                log.warning("cross_check failed for F%s: %s", f.id, e)
                return None

    checks_raw = await asyncio.gather(*[_check_one(f) for f in findings])
    cross_checks = [c for c in checks_raw if c is not None]

    if not cross_checks:
        # Total cross-check failure. Synthesize a defensive empty batch so
        # the caller can decide what to do (typically: skip and let the
        # watchdog re-trigger).
        log.warning("audit T%s: all %d cross-checks failed; returning empty",
                    task.id, len(findings))
        empty = AuditBatch(scores=[])
        # Return run_id=0 sentinel; caller treats <=0 as "no audit run anchor".
        return empty, 0

    # ---- 2. batch_score ----------------------------------------------
    batch_prompt = await curator.build(
        invocation_type="auditor.batch_score",
        context={
            "task": task,
            "findings": findings,
            "cross_checks": [c.model_dump() for c in cross_checks],
        },
    )
    batch, batch_run_id = await router.invoke(
        prompt=batch_prompt,
        output_schema_class=AuditBatch,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="batch_score",
        parent_step_id=None,
    )
    return batch, batch_run_id
