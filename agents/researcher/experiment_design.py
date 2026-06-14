"""The researcher AUTHORS an experiment — `experiment.requested` → design a script and queue it.

This is the design half of the lab's hands, moved under the researcher (migration 022): the direction's
OWNER writes the experiment in their own full-stack voice (identity.system_prompt), not a generic
"experimentalist". Because this module lives at agents.researcher.*, the dispatcher's mode dial gates
it on the `researcher` agent (agent_of) — design pauses when the researcher is off/shadow, as it should.

Flow: resolve the owner → build the design prompt with their persona + full context → (the prompt
builder + recipe still live in agents.experiments.handler, the shared "design language") → PRE-FLIGHT
the generated code statically and cheaply repair it before spending a container slot → queue it for the
Quartermaster (which owns the off-slot run→debug→rerun loop). Interpretation is the sibling
experiment_interpret module.
"""

from __future__ import annotations

import hashlib
import logging

from agents.experiments import preflight, sandbox
from agents.experiments.handler import _LAB_CONSTRAINTS
from agents.experiments.schemas import ExperimentDesign
from agents.researcher.identity import load_researcher, system_prompt

log = logging.getLogger(__name__)

PREFLIGHT_MAX_FIXES = 2  # cheap in-process repairs before a container slot is spent


def _capability_gap(reason: str) -> str:
    """Classify WHY a hypothesis is infeasible into a capability the lab is missing, so the gap can
    feed back to Ariadne (a constraint update) or ops (a zoo/dataset addition) instead of dead-ending."""
    r = (reason or "").lower()
    if "cross-encoder" in r or "cross encoder" in r or "rerank" in r:
        return "cross_encoder"
    if "logprob" in r or "perplexit" in r:
        return "logprobs"
    if "fine-tun" in r or "finetun" in r:
        return "fine_tuning"
    if "pretrained" in r or "checkpoint" in r or "hub" in r or "weights" in r:
        return "pretrained_model"
    if "network" in r or "internet" in r or "download" in r or "fetch" in r:
        return "network"
    if "dataset" in r or "data" in r:
        return "dataset"
    return "other"


async def _preflight_repair(dispatcher, design: ExperimentDesign, hypothesis: str, claim_statement: str):
    """Statically check the design's code and, while it has avoidable problems (banned imports, bad
    /data paths, no result print), ask the debug recipe to fix it IN-PROCESS — no container. Returns
    the (possibly repaired) design; gives up after PREFLIGHT_MAX_FIXES, leaving the QM's debug loop
    to handle anything static checks can't see."""
    for attempt in range(1, PREFLIGHT_MAX_FIXES + 1):
        problems = preflight.check(design.code)
        if not problems:
            return design
        log.info("preflight: exp design has %d problem(s), repair %d: %s", len(problems), attempt, problems[0])
        prompt = await dispatcher.curator.build(
            invocation_type="experiments.debug",
            context={
                "code": design.code,
                "error": "Pre-flight checks (no run yet) found problems:\n- " + "\n- ".join(problems),
                "hypothesis": hypothesis,
                "claim_statement": claim_statement,
                "iteration": attempt,
            },
        )
        fixed, _run_id = await dispatcher.router.invoke(
            prompt=prompt,
            output_schema_class=ExperimentDesign,
            triggered_by_event_id=None,
            session=None,
            step_name="experiments.debug",
        )
        # Keep the original hypothesis/budgets; take the repaired code + any escalated needs.
        design.code = fixed.code
        design.requires_gpu = design.requires_gpu or fixed.requires_gpu
    return design


async def handle_experiment_requested(event: dict, dispatcher) -> dict | None:
    """`experiment.requested` → the owning researcher designs a script and queues it for the QM.

    Payload: {claim_id, task_id?, inquiry_id?, hypothesis?, goal?, require_real_data?, researcher_id?}.
    A `task_id` is required (the queued experiment_runs row FKs to a task)."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    claim_id = payload.get("claim_id")
    task_id = payload.get("task_id")
    if task_id is None:
        return {"skipped": True, "reason": "no task_id in experiment.requested payload"}

    # The owning researcher (migration 022): the task's owner, falling back to the direction's.
    researcher_id = payload.get("researcher_id")
    if researcher_id is None:
        researcher_id = await state.pool.fetchval("SELECT researcher_id FROM tasks WHERE id = $1", task_id)
    if researcher_id is None and claim_id is not None:
        researcher_id = await state.pool.fetchval("SELECT researcher_id FROM claims WHERE id = $1", claim_id)
    researcher = await load_researcher(state.pool, researcher_id)

    claim_statement = ""
    prior_hypotheses: list[str] = []
    if claim_id is not None:
        try:
            claim = await state.get_claim(claim_id)
            claim_statement = claim.statement
        except ValueError:
            claim_statement = ""
        # Prior experiments on this direction → test a DISTINCT facet, so a driven series accumulates a
        # varied picture (ablations/sweeps), not near-copies.
        try:
            prior = await state.get_completed_experiments_for_claim(claim_id, limit=10)
            prior_hypotheses = [h for e in prior if (h := (e.get("params") or {}).get("hypothesis"))]
        except Exception:  # noqa: BLE001 — best-effort context
            prior_hypotheses = []

    proposal_hypotheses: list[dict] = []
    if claim_id is not None and hasattr(state, "get_research_document"):
        try:
            proposal = await state.get_research_document(claim_id, "proposal")
            if proposal:
                proposal_hypotheses = (proposal.get("meta") or {}).get("hypotheses") or []
        except Exception:  # noqa: BLE001 — the proposal is best-effort context
            log.exception("experiments: failed to load proposal for claim %s", claim_id)

    require_real_data = bool(payload.get("require_real_data"))  # A5: confirm a synthetic pilot on REAL data
    prompt = await dispatcher.curator.build(
        invocation_type="experiments.design",
        context={
            "hypothesis": payload.get("hypothesis") or "",
            "goal": payload.get("goal") or "",
            "claim_statement": claim_statement,
            "lab_constraints": _LAB_CONSTRAINTS,
            "prior_hypotheses": prior_hypotheses,
            "proposal_hypotheses": proposal_hypotheses,
            "require_real_data": require_real_data,
            "researcher_persona": system_prompt(researcher) if researcher else "",
        },
    )
    design, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ExperimentDesign,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="experiments.design",
    )

    if design.infeasible:
        # The honest dead-end: the hypothesis can't be COMPUTED in the offline sandbox; simulating it
        # would be fabricated evidence. Record a failed attempt with the reason (Ariadne's reflect reads
        # WHY); for a real-data confirmation, surface the missing dataset as a concrete signal.
        reason = (design.infeasible_reason or "not computable with the offline sandbox stack").strip()
        exp_id = await state.queue_experiment(
            task_id=task_id,
            inquiry_id=payload.get("inquiry_id"),
            kind="code",
            params={"hypothesis": design.hypothesis, "claim_id": claim_id, "infeasible": True},
            code="",
            wall_clock_budget_s=0,
            mem_budget_mb=0,
            requires_gpu=False,
            gpu_mem_mb=None,
            priority=0,
            provenance={"infeasible": True},
            dataset_refs=None,
            researcher_id=researcher_id,
        )
        await state.record_experiment_result(
            exp_id, status="failed", error=f"infeasible on lab sandbox: {reason}", failure_class="infeasible"
        )
        await state.set_experiment_interpretation(
            exp_id,
            None,
            None,
            f"Untestable on the lab's offline sandbox: {reason}. The direction needs capabilities the "
            "sandbox lacks (pretrained models / network / external data) — fabricating a simulation "
            "instead would not bear on the claim.",
        )
        log.info("experiments: design INFEASIBLE for claim %s (task %s): %s", claim_id, task_id, reason[:120])
        # Surface the gap as a concrete signal (deduped per day per claim). A real-data confirmation that
        # can't be met stays `needs_real_dataset` (feeds dataset curation); anything else is a general
        # `needs_capability` carrying the missing capability so Ariadne's reflect / ops can act on it.
        day = await state.pool.fetchval("SELECT to_char(now(), 'YYYY-MM-DD')")
        if require_real_data and claim_id is not None:
            await state.emit_corpus_event(
                "loop.unclosed",
                target_type="system",
                target_id=0,
                payload={"kind": "needs_real_dataset", "claim_id": claim_id, "reason": reason[:300]},
                dedup_key=f"needs-real-dataset-{claim_id}-{day}",
            )
        elif claim_id is not None:
            cap = _capability_gap(reason)
            await state.emit_corpus_event(
                "loop.unclosed",
                target_type="system",
                target_id=0,
                payload={"kind": "needs_capability", "capability": cap, "claim_id": claim_id, "reason": reason[:300]},
                dedup_key=f"needs-capability-{cap}-{claim_id}-{day}",
            )
        return {"infeasible": True, "experiment_id": exp_id, "claim_id": claim_id, "reason": reason}

    # PRE-FLIGHT: cheaply repair avoidable problems before a container slot is spent.
    design = await _preflight_repair(dispatcher, design, design.hypothesis, claim_statement)

    code_hash = hashlib.sha256(design.code.encode()).hexdigest()[:16]
    provenance = {
        "image": sandbox.IMAGE,
        "image_digest": await sandbox.image_digest(),
        "seed": design.seed,
        "code_hash": code_hash,
        "dataset_plan": design.dataset_plan,
        "synthesis_justification": design.synthesis_justification,
    }
    exp_id = await state.queue_experiment(
        task_id=task_id,
        inquiry_id=payload.get("inquiry_id"),
        kind="code",
        params={
            "hypothesis": design.hypothesis,
            "claim_id": claim_id,
            "dataset_plan": design.dataset_plan,
            "synthesis_justification": design.synthesis_justification,
        },
        code=design.code,
        # Floor at 300s: the session budget is shared across retries; a low floor killed real GPU runs
        # mid-attempt (model load + dataset inference needs room before the first attempt finishes).
        wall_clock_budget_s=max(300, min(1800, design.est_wall_clock_s)),
        mem_budget_mb=design.est_mem_mb,
        requires_gpu=design.requires_gpu,
        gpu_mem_mb=design.gpu_mem_mb or (4096 if design.requires_gpu else None),
        priority=6,
        provenance=provenance,
        dataset_refs=None,
        researcher_id=researcher_id,
    )
    who = researcher.name if researcher else "researcher"
    log.info("%s: queued exp %s for claim %s (task %s, gpu=%s)", who, exp_id, claim_id, task_id, design.requires_gpu)
    return {"queued_experiment": exp_id, "claim_id": claim_id, "design_run_id": run_id, "researcher_id": researcher_id}
