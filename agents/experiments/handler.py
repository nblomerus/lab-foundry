"""
The experiments agent — the lab's hands.

When literature can't settle a number, the Researcher parks a direction with
`needs_experiment`; this agent turns that into a small, reproducible run:

    experiment.requested  →  design a self-contained script  →  queue it
                          (the Quartermaster runs it in the sandbox)
    experiment.completed  →  interpret the numbers  →  nudge the direction's
                          confidence + ingest a first-party lab note into the Library
    experiment.failed     →  record the failure as data (a killed/failed run is also a result)

Two LLM steps bookend the sandboxed run — `experiments.design` (write the code)
and `experiments.interpret` (read the result honestly). Both register their
curator recipe + route tier at module import (idempotent, like the researcher
loop). The mode-dial agent name is `experiments` (this module's path).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from agents.experiments import sandbox
from agents.experiments.schemas import ExperimentDesign, ExperimentReport
from agents.synthesis.handler import SYNTHESIS_MIN_EXPERIMENTS, SYNTHESIS_RESYNTH_STEP
from harness.curator import RECIPES, SYSTEM_PROMPTS, PromptLayer, Recipe
from harness.router import ROUTE, Tier

log = logging.getLogger(__name__)

# The lab's compute envelope — pulled from Ariadne so a designed experiment fits the same
# hardware her directions are framed against. Falls back to a terse 2-line version if the
# export ever moves (imports stay at module top per repo style).
try:
    from agents.ariadne.loop import LAB_CONSTRAINTS as _LAB_CONSTRAINTS
except ImportError:  # pragma: no cover — defensive; the export exists today
    _LAB_CONSTRAINTS = (
        "OFFLINE sandbox: no network, no pretrained weights, no external data. Stack: numpy/scipy/"
        "pandas/scikit-learn/xgboost/statsmodels/torch (CPU + one modest GPU, from-scratch models only). "
        "Favour classical ML / from-scratch torch / algorithmic claims; synthesize inputs or use the "
        "stack's toy datasets. NEVER simulate the phenomenon under test."
    )


# -------------------------------------------------------------------------
# Curator task_data builders
# -------------------------------------------------------------------------


def _llm_endpoint_blocks() -> tuple[str, str, str]:
    """The design prompt's local-model capability section — present only while the
    inference broker is up (EXPERIMENT_LLM_BROKER), read at call time so a flip doesn't
    need a reimport. Returns (llm_block, llm_import_suffix, infeasible_cannot_clause)."""
    if os.environ.get("EXPERIMENT_LLM_BROKER", "").lower() not in {"on", "1", "true"}:
        return (
            "",
            "",
            "it needs a pretrained LLM's behaviour, network access, or an external dataset",
        )
    models = os.environ.get(
        "EXPERIMENT_LLM_MODELS",
        "mistral:7b-instruct-q4_K_M, qwen2.5:14b-instruct-q4_K_M, qwen2.5-coder:7b, nomic-embed-text",
    )
    block = f"""
## Local model endpoint — REAL model behaviour IS testable
The sandbox mounts a lab helper at /opt/lab/llm.py — the ONLY model access (a brokered
local Ollama over a unix socket; the sandbox still has NO network). Local models: {models}.
    import sys; sys.path.insert(0, "/opt/lab"); import llm
    text = llm.generate("mistral:7b-instruct-q4_K_M", prompt, temperature=0.0, seed=seed)
    reply = llm.chat(model, [{{"role": "user", "content": "..."}}], temperature=0.8)
    vecs = llm.embed("nomic-embed-text", [s1, s2])
Use it to MEASURE real model behaviour: sampling/decoding strategies, self-consistency,
calibration, prompt-format effects, embedding geometry. Calls serialize with the lab's
other GPU work — roughly seconds per 7B call, tens of seconds at 14B+. Budget honestly:
n_calls × per-call seconds must fit est_wall_clock_s (cap 1800); prefer ≤7B models and a
modest call count. Generate probe INPUTS in-code (arithmetic/logic problems with known
answers are fine); model OUTPUTS must come from llm.* calls, never be fabricated.
"""
    return (
        block,
        " (plus the mounted /opt/lab/llm.py helper described above)",
        "it needs models beyond the local zoo, fine-tuning or training of pretrained weights, "
        "network access, or an external dataset",
    )


def _datasets_block() -> str:
    """The design prompt's /data section, rendered from the benchmark pack's manifest
    (ops.build_benchmark_pack) at call time — absent pack, absent section."""
    manifest_path = os.path.join(sandbox.DATASETS_DIR, "manifest.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except OSError:
        return ""
    if not manifest:
        return ""
    lines = "\n".join(
        f"- /data/{d['file']} — {d['n']} rows; {d['task']}; fields: {d['fields']} ({d['license']})" for d in manifest
    )
    return f"""
## Benchmark datasets mounted READ-ONLY at /data (offline, license-clean)
{lines}
Load with:  import json;  rows = [json.loads(line) for line in open("/data/gsm8k_test.jsonl")]
When the hypothesis concerns benchmark-type behaviour, PREFER a real benchmark slice over
in-code generated probes — sample a SEEDED subset sized to your wall-clock budget (e.g.
50-200 rows), and report which dataset, slice size, and seed in the result's `dataset`.
"""


async def _build_design(ctx: dict, state, memory) -> PromptLayer:
    hypothesis = ctx.get("hypothesis") or ""
    goal = ctx.get("goal") or ""
    claim_statement = ctx.get("claim_statement") or ""
    lab_constraints = ctx.get("lab_constraints") or _LAB_CONSTRAINTS
    prior_hypotheses = ctx.get("prior_hypotheses") or []
    proposal_hypotheses = ctx.get("proposal_hypotheses") or []
    llm_block, llm_import, cannot = _llm_endpoint_blocks()
    proposal_block = ""
    if proposal_hypotheses:
        hyp_lines = "\n".join(
            f"- {h.get('hid', 'H?')}: {h.get('statement', '')} "
            f"(metric: {h.get('metric', '')}; decision: {h.get('threshold', '')})"
            for h in proposal_hypotheses
        )
        proposal_block = (
            "## The research PROPOSAL's hypotheses (the PI's plan — test the NEXT untested one)\n"
            f"{hyp_lines}\n"
            "Pick the first hypothesis NOT already covered by the prior experiments above, set "
            "`hypothesis` to ITS statement (keep its hid prefix, e.g. 'H2: ...'), and design the "
            "experiment that decides it by ITS metric and threshold.\n\n"
        )
    data_block = _datasets_block()
    data_hint = " or a /data benchmark slice (see above)" if data_block else ""
    prior_block = (
        "## Experiments ALREADY run on this direction — test a DISTINCT facet, do NOT repeat these\n"
        + "\n".join(f"- {h}" for h in prior_hypotheses)
        + "\nDesign the NEXT experiment in the series: vary a factor, ablate a component, change the "
        "dataset/model/metric, or probe a boundary the prior runs did not — so the accumulated runs "
        "build a richer picture of the direction, not a repeat.\n\n"
        if prior_hypotheses
        else ""
    )

    content = f"""## Direction under test
{claim_statement or "(no direction statement)"}

## Goal of the task that spawned this
{goal or "(no explicit goal)"}

## Hypothesis to test
{hypothesis or "(none stated — derive the most load-bearing testable claim from the direction)"}

{prior_block}{proposal_block}## Lab compute envelope (the experiment MUST fit this)
{lab_constraints}

---

Design ONE small, reproducible experiment that produces a NUMBER bearing on the hypothesis —
something literature can't settle by reading. Stay inside the lab envelope: a single modest GPU,
local models ≤ ~32B. Favour inference-time methods, evaluation / benchmarking, ablations, and
small statistical studies. Do NOT propose large training runs, multi-GPU work, network calls,
or external data fetches.

The experiment MUST test a SUBSTANTIVE, FALSIFIABLE ML CLAIM about the METHOD in the direction —
a concrete prediction with a measurable threshold, e.g. "sparse GP with 50 inducing points keeps
test R² within 0.02 of the full GP at 10× lower cost" or "random Fourier features match the RBF
kernel's AUC to within 0.01 at D=500 features". State the claim, the metric, and what number would
FALSIFY it. The result must measure THAT claim directly.
HARD RULES — reject these and derive a real claim from the direction instead:
- NO meta-hypotheses about the lab's own machinery ("can the system generate a hypothesis",
  "hypothesis generation is absent/trivial", "no results exist yet"). Those are not experiments.
- NO generic proxy benchmarks (e.g. plain accuracy on a fresh make_classification) UNLESS that
  metric IS the claim. A number that doesn't discriminate the hypothesis is worthless.
- STRESS THE REGIME THE CLAIM IS ABOUT — design the experiment so the claim COULD fail. If the
  hypothesis says "low-data", use genuinely small n; if "miscalibration", pick a setting where
  miscalibration is plausible (noise, shift, model mismatch), not a toy where every method is
  trivially perfect. Two arms that both succeed easily discriminate NOTHING (we learned this:
  an exact-vs-sparse GP test on a clean 1D toy returned ECE 0.0009 for both — zero information).
- NEVER SIMULATE THE PHENOMENON UNDER TEST. If your code ASSUMES the behaviour being measured —
  e.g. drawing "model answers" from an invented per-sample accuracy and then "measuring" that
  voting improves accuracy — the conclusion was baked into the input: fabricated evidence, worse
  than no experiment. The script must COMPUTE the phenomenon: actually fit/train/run the model or
  algorithm on data. Synthetic INPUTS are fine; synthetic OUTCOMES are not.
- If the hypothesis cannot be COMPUTED with what the sandbox offers — {cannot} — do NOT
  approximate it. Set `infeasible` true, say why in `infeasible_reason`, set `code` to "".
  An honest infeasible beats fake support.
If the stated hypothesis is meta/degenerate, DERIVE the most load-bearing testable claim about the
direction's method and test that — name it explicitly in `hypothesis`.
{llm_block}{data_block}
Write `code` as a COMPLETE, self-contained Python script:
- Import ONLY the preinstalled stack: numpy, scipy, pandas, scikit-learn, xgboost, statsmodels, torch{llm_import}.
- NO network; file reads only from the cwd and the read-only /data mount. Synthesize your data, use
  a sklearn/torch toy dataset (e.g. make_classification, load_digits, a small random tensor){data_hint}.
  State your data source in `dataset_plan` (be specific: the loader/generator, its parameters, the
  slice + seed — this is the dataset's reproducibility record).
- Seed every RNG you touch (numpy, torch, python `random`) from `seed` so the run reproduces.
- Keep it within the wall-clock and memory budgets you estimate. Modest is better than ambitious —
  a clean signal on a toy problem beats a run that times out.
- In your result JSON, include a `dataset` object capturing what you built/used so it's reproducible
  and inspectable: {{"n_samples", "n_features" (or shape), "source" (the generator/loader call),
  "sha256" (hashlib.sha256 of the data bytes, e.g. of X.tobytes())}}.
- The script's LAST stdout line MUST be a single JSON object = the result (the numbers you want
  interpreted: metrics, deltas, counts, p-values, timings — plus the `dataset` object). Print nothing after it.

Set `requires_gpu` true ONLY if the run genuinely needs the GPU. Cap `est_wall_clock_s` at 1800.
Return JSON conforming to ExperimentDesign.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_interpret(ctx: dict, state, memory) -> PromptLayer:
    kind = ctx.get("kind") or "code"
    params = ctx.get("params") or {}
    result = ctx.get("result")
    hypothesis = ctx.get("hypothesis") or ""
    claim_statement = ctx.get("claim_statement") or ""

    content = f"""## Direction under test
{claim_statement or "(no direction statement)"}

## Hypothesis the experiment tested
{hypothesis or "(none recorded)"}

## Experiment ({kind})
**Params:** {json.dumps(params)[:1000]}

## Result (the script's JSON output)
```json
{json.dumps(result, indent=2)[:6000]}
```

---

Interpret this result HONESTLY against the hypothesis and the direction:
- `summary`: 2-4 sentences naming the ACTUAL numbers — the metric, the delta, the comparison.
- `confidence`: 0..1, how load-bearing this single run is. A clean signal on a toy problem is
  suggestive, not decisive — calibrate down for small/synthetic studies.
- `narrative_note`: a first-person lab note — what you hypothesized, what you ran, what you
  observed, any surprises, what it means for the direction, and the single most useful next step.
- `supports_direction`: true if the result backs the direction, false if it pushes against it,
  null if neutral / inconclusive.
- `confidence_delta`: how to move the direction's confidence, −0.3..+0.3. Use 0 when the result is
  neutral, null, or dominated by noise. A null result is also data — say so plainly; don't invent a
  signal to justify a move.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_debug(ctx: dict, state, memory) -> PromptLayer:
    """The DEBUG skill: the experiment's code errored or produced no usable result —
    read the code + the failure and fix it (the iterative half of the coding loop)."""
    code = ctx.get("code") or ""
    error = ctx.get("error") or "(no error captured)"
    hypothesis = ctx.get("hypothesis") or ""
    claim_statement = ctx.get("claim_statement") or ""
    iteration = ctx.get("iteration") or 1
    lab_constraints = ctx.get("lab_constraints") or _LAB_CONSTRAINTS

    content = f"""## Debugging an experiment (attempt {iteration})
You wrote a Python experiment to test a hypothesis; it FAILED to produce a usable result.
Read your own code and the failure, find the bug, and return CORRECTED code.

## Direction under test
{claim_statement or "(no direction statement)"}

## Hypothesis
{hypothesis or "(none stated)"}

## Lab compute envelope (the fix MUST still fit this)
{lab_constraints}

## The code that failed
```python
{code[:8000]}
```

## What went wrong (stderr / traceback / reason)
```
{error[:4000]}
```

---

Diagnose the actual failure (an import error, a shape/type mismatch, an API misuse, a NaN/inf,
a timeout because it was too slow, or no JSON printed) and FIX it. Common fixes:
- ImportError → use only numpy/scipy/pandas/scikit-learn/xgboost/statsmodels/torch.
- Timeout (the run was killed) → make it cheaper: fewer samples/iterations, a smaller model.
- No result → ensure the LAST stdout line is a single JSON object and nothing prints after it.
- Wrong numbers → fix the logic so the metric actually bears on the hypothesis.
NEVER "fix" a failure by replacing real computation with a simulation of the expected outcome
(e.g. an unavailable model swapped for sampled answers at an assumed accuracy) — if the real
computation is impossible in this sandbox, return `infeasible` true instead of fake code.

Return the COMPLETE corrected script in `code` (not a diff), keeping it self-contained, seeded,
network-free, and inside the budgets. Conform to ExperimentDesign (reuse the same hypothesis).
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe + route + system-prompt registration (idempotent — guard double-import)
# -------------------------------------------------------------------------

SYSTEM_PROMPTS.setdefault(
    "experiments",
    (
        "You are an experimentalist in an autonomous AI research lab. You write small, reproducible, "
        "self-contained Python experiments that run with no network and no external data, and you "
        "interpret their numeric results honestly. You favour clean toy studies over ambitious runs "
        "that won't fit the lab's hardware. A null or failed result is data, not a setback — you never "
        "inflate a weak signal into a conclusion."
    ),
)

_EXPERIMENT_RECIPES: list[tuple[str, str, int, str, object]] = [
    (
        "experiments.design",
        "Design ONE small, self-contained, reproducible experiment for a direction under test.",
        12_000,
        "ExperimentDesign",
        _build_design,
    ),
    (
        "experiments.debug",
        "Read the experiment's failed code + traceback and return corrected code (the debug loop).",
        12_000,
        "ExperimentDesign",
        _build_debug,
    ),
    (
        "experiments.interpret",
        "Interpret a completed experiment's numeric result against the hypothesis and direction.",
        8_000,
        "ExperimentReport",
        _build_interpret,
    ),
]

for _itype, _desc, _budget, _schema, _builder in _EXPERIMENT_RECIPES:
    if _itype not in RECIPES:
        RECIPES[_itype] = Recipe(
            invocation_type=_itype,
            description=_desc,
            agent="experiments",
            total_budget=_budget,
            use_cold_path=False,
            recall_sessions=[],
            recall_k=0,
            output_schema=_schema,
            task_data_builder=_builder,
        )

# The code design + debug loop is a complex task → the dedicated EXPERIMENT tier
# (DeepSeek v4 Flash lead, local coder fallback). Interpretation is plain reasoning → WORKHORSE.
ROUTE.setdefault("experiments.design", Tier.EXPERIMENT)
ROUTE.setdefault("experiments.debug", Tier.EXPERIMENT)
ROUTE.setdefault("experiments.interpret", Tier.WORKHORSE)


# -------------------------------------------------------------------------
# Handlers
# -------------------------------------------------------------------------


async def handle_experiment_requested(event: dict, dispatcher) -> dict | None:
    """`experiment.requested` → design a script and queue it for the Quartermaster.

    Payload: {claim_id, task_id?, inquiry_id?, hypothesis?, goal?}. We require a `task_id`
    (the caller — the grounded researcher — has it in scope and provides it); the queued
    experiment_runs row FKs to a task, so we can't queue without one.
    """
    state = dispatcher.state
    payload = event.get("payload") or {}
    claim_id = payload.get("claim_id")
    task_id = payload.get("task_id")
    if task_id is None:
        return {"skipped": True, "reason": "no task_id in experiment.requested payload"}

    claim_statement = ""
    prior_hypotheses: list[str] = []
    if claim_id is not None:
        try:
            claim = await state.get_claim(claim_id)
            claim_statement = claim.statement
        except ValueError:
            claim_statement = ""
        # Prior experiments on this direction → tell the designer to test a DISTINCT facet, so a
        # driven series accumulates a varied picture (ablations/sweeps), not three near-copies.
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

    prompt = await dispatcher.curator.build(
        invocation_type="experiments.design",
        context={
            "hypothesis": payload.get("hypothesis") or "",
            "goal": payload.get("goal") or "",
            "claim_statement": claim_statement,
            "lab_constraints": _LAB_CONSTRAINTS,
            "prior_hypotheses": prior_hypotheses,
            "proposal_hypotheses": proposal_hypotheses,
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
        # The honest dead-end: the hypothesis cannot be COMPUTED in the offline sandbox and
        # simulating it would be fabricated evidence. Record a failed attempt with the reason
        # as the run's note — the coverage driver counts attempts (its give-up cap stops
        # re-driving the direction), the spine re-armer sees a handled run (no churn), and
        # Ariadne's reflect reads WHY the direction can't be tested on this hardware.
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
        )
        await state.record_experiment_result(exp_id, status="failed", error=f"infeasible on lab sandbox: {reason}")
        await state.set_experiment_interpretation(
            exp_id,
            None,
            None,
            f"Untestable on the lab's offline sandbox: {reason}. The direction needs capabilities "
            "the sandbox lacks (pretrained models / network / external data) — fabricating a "
            "simulation instead would not bear on the claim.",
        )
        log.info("experiments: design INFEASIBLE for claim %s (task %s): %s", claim_id, task_id, reason[:120])
        return {"infeasible": True, "experiment_id": exp_id, "claim_id": claim_id, "reason": reason}

    code_hash = hashlib.sha256(design.code.encode()).hexdigest()[:16]
    # Provenance = the reproducibility basis. Capture the image DIGEST (immutable),
    # not just the tag (a rebuild repoints it), alongside seed + code hash. With
    # these + the code, the run — including its synthesized dataset — is recreatable.
    provenance = {
        "image": sandbox.IMAGE,
        "image_digest": await sandbox.image_digest(),
        "seed": design.seed,
        "code_hash": code_hash,
        "dataset_plan": design.dataset_plan,
    }
    exp_id = await state.queue_experiment(
        task_id=task_id,
        inquiry_id=payload.get("inquiry_id"),
        kind="code",
        params={"hypothesis": design.hypothesis, "claim_id": claim_id, "dataset_plan": design.dataset_plan},
        code=design.code,
        # Floor at 120s: designs have estimated 10-60s and the session budget killed runs
        # before a single attempt finished ("session budget 10s exhausted after 0 attempt(s)").
        wall_clock_budget_s=max(120, min(1800, design.est_wall_clock_s)),
        mem_budget_mb=design.est_mem_mb,
        requires_gpu=design.requires_gpu,
        gpu_mem_mb=design.gpu_mem_mb or (4096 if design.requires_gpu else None),
        priority=6,
        provenance=provenance,
        dataset_refs=None,
    )
    log.info("experiments: queued exp %s for claim %s (task %s, gpu=%s)", exp_id, claim_id, task_id, design.requires_gpu)
    return {"queued_experiment": exp_id, "claim_id": claim_id, "design_run_id": run_id}


async def handle_experiment_completed(event: dict, dispatcher) -> dict | None:
    """`experiment.completed` → interpret the result, nudge the direction, ingest a lab note."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    experiment_id = payload.get("experiment_id")
    claim_id = payload.get("claim_id")

    exp = await state.get_experiment(experiment_id) if experiment_id is not None else None
    if exp is None or not exp.get("result"):
        return {"skipped": True, "reason": "experiment missing or has no result", "experiment_id": experiment_id}

    params = exp.get("params") or {}
    hypothesis = params.get("hypothesis") or ""

    claim_statement = ""
    if claim_id is not None:
        try:
            claim = await state.get_claim(claim_id)
            claim_statement = claim.statement
        except ValueError:
            claim_statement = ""

    prompt = await dispatcher.curator.build(
        invocation_type="experiments.interpret",
        context={
            "kind": exp.get("kind") or "code",
            "params": params,
            "result": exp["result"],
            "hypothesis": hypothesis,
            "claim_statement": claim_statement,
        },
    )
    report, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ExperimentReport,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="experiments.interpret",
    )

    await state.set_experiment_interpretation(experiment_id, report.summary, run_id, report.narrative_note)

    # Move the direction's confidence — best-effort: an inactive claim can't be steered.
    conf_applied = None
    if claim_id is not None and report.confidence_delta:
        try:
            claim = await state.get_claim(claim_id)
            new_conf = max(0.0, min(1.0, claim.confidence + report.confidence_delta))
            await state.update_claim_confidence(
                claim_id,
                new_conf,
                reason=f"experiment {experiment_id}: {report.summary[:120]}",
                run_id=run_id,
            )
            conf_applied = [round(float(claim.confidence), 3), round(new_conf, 3)]
        except ValueError as e:  # claim not found or not active — the experiment still stands
            log.warning("experiments: confidence move skipped for claim %s: %s", claim_id, e)

    # Ingest a first-party lab note into the Library so Mimir / the corpus carry what the lab ran.
    text = _lab_note_markdown(experiment_id, claim_id, hypothesis, exp, report)
    await state.emit_corpus_event(
        "source.discovered",
        target_type="source",
        target_id=experiment_id,
        payload={
            "source": {
                "kind": "note",
                "source_kind": "lab_experiment",
                "canonical_key": f"exp:{experiment_id}",
                "title": f"Experiment {experiment_id}",
                "why": "first-party lab experiment",
            },
            "content": text,
            "provenance": exp.get("provenance") or {},
        },
        dedup_key=f"exp-doc-{experiment_id}",
    )

    # Capture the DATASET as its own first-party Library doc so the corpus carries how
    # the data was assembled (and how to regenerate it) — the loop's reproducibility
    # record for the inputs, not just the result. The dataset's content sha256 (the
    # script reports it) goes into the card's provenance so Mimir's lab_dataset trust
    # gate can certify it (a hash present = the bytes are pinned), not quarantine it.
    result = exp.get("result") or {}
    ds_fp = result.get("dataset") or result.get("datasets") or {}
    ds_sha = ds_fp.get("sha256") if isinstance(ds_fp, dict) else None
    dataset_provenance = {**(exp.get("provenance") or {}), "sha256": ds_sha}
    dataset_key = f"dataset:exp:{experiment_id}"
    await state.emit_corpus_event(
        "source.discovered",
        target_type="source",
        target_id=experiment_id,
        payload={
            "source": {
                "kind": "dataset",
                "source_kind": "lab_dataset",
                "canonical_key": dataset_key,
                "title": f"Dataset · experiment {experiment_id}",
                "why": "first-party lab experiment dataset",
            },
            "content": _lab_dataset_markdown(experiment_id, claim_id, exp),
            "provenance": dataset_provenance,
        },
        dedup_key=f"exp-dataset-{experiment_id}",
    )
    await state.set_experiment_dataset_refs(
        experiment_id,
        [
            {
                "canonical_key": dataset_key,
                "kind": "lab_dataset",
                "sha256": ds_sha,
                "plan": (exp.get("provenance") or {}).get("dataset_plan"),
                "fingerprint": ds_fp or None,
            }
        ],
    )

    # Condition-driven: once a direction has accumulated enough completed experiments, trigger the
    # terminal cross-experiment SYNTHESIS (the paper-shaped finding). Bucketed dedup so it fires once
    # per RESYNTH_STEP runs — not on every completion — and re-fires when materially more evidence lands.
    synth_triggered = False
    if claim_id is not None:
        n_done = await state.count_completed_experiments_for_claim(claim_id)
        if n_done >= SYNTHESIS_MIN_EXPERIMENTS:
            bucket = n_done // max(1, SYNTHESIS_RESYNTH_STEP)
            await state.emit_corpus_event(
                "finding.synthesize",
                target_type="claim",
                target_id=claim_id,
                payload={"claim_id": claim_id, "experiment_count": n_done},
                dedup_key=f"synthesize-{claim_id}-{bucket}",
            )
            synth_triggered = True

    log.info(
        "experiments: interpreted exp %s (claim %s, supports=%s Δconf=%s, synth=%s)",
        experiment_id,
        claim_id,
        report.supports_direction,
        report.confidence_delta,
        synth_triggered,
    )
    return {
        "experiment_id": experiment_id,
        "claim_id": claim_id,
        "interpret_run_id": run_id,
        "supports_direction": report.supports_direction,
        "confidence": conf_applied,
        "ingested_note": True,
        "ingested_dataset": True,
        "synthesis_triggered": synth_triggered,
    }


async def handle_experiment_failed(event: dict, dispatcher) -> dict | None:
    """`experiment.failed` → record the failure as a researcher note. A killed/failed run is data
    too: the approach as written didn't produce a usable result. No confidence move."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    experiment_id = payload.get("experiment_id")
    if experiment_id is None:
        return {"skipped": True, "reason": "no experiment_id in experiment.failed payload"}

    exp = await state.get_experiment(experiment_id)
    error = (exp.get("error") if exp else None) or "(no error recorded)"
    note = (
        f"Experiment failed: {error}. A failed/killed run is also data — the approach as written "
        "didn't produce a usable result."
    )
    await state.set_experiment_interpretation(experiment_id, None, None, note)
    log.info("experiments: recorded failure for exp %s: %s", experiment_id, error[:120])
    return {"experiment_id": experiment_id, "handled": True, "failed": True}


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _provenance_line(provenance: dict) -> str:
    """One-line reproducibility stamp: seed + image digest + code hash."""
    return (
        f"seed={provenance.get('seed')} "
        f"image={provenance.get('image')}@{(provenance.get('image_digest') or '?')} "
        f"code_hash={provenance.get('code_hash')}"
    )


def _lab_note_markdown(experiment_id: int, claim_id, hypothesis: str, exp: dict, report: ExperimentReport) -> str:
    """A markdown lab note for the corpus — the human-readable record of one experiment."""
    params = exp.get("params") or {}
    provenance = exp.get("provenance") or {}
    result = exp.get("result")
    direction = f"T{claim_id}" if claim_id is not None else "(no direction)"
    return (
        f"## Experiment {experiment_id} on direction {direction}\n\n"
        f"**Hypothesis:** {hypothesis or '(none recorded)'}\n\n"
        f"**Dataset:** {provenance.get('dataset_plan') or '(synthesized in-code)'}\n\n"
        f"**Method / params:** `{json.dumps(params)[:600]}`\n"
        f"{_provenance_line(provenance)}\n\n"
        f"**Result:**\n```json\n{json.dumps(result, indent=2)[:2000]}\n```\n\n"
        f"**Interpretation:** {report.summary}\n\n"
        f"**Supports direction:** {report.supports_direction}  |  "
        f"Δconfidence: {report.confidence_delta}\n\n"
        f"**Researcher note:** {report.narrative_note}\n"
    )


def _lab_dataset_markdown(experiment_id: int, claim_id, exp: dict) -> str:
    """A markdown dataset card for the corpus — how the experiment's data was assembled,
    captured so the dataset is reproducible (regenerate from the code+seed+image digest)
    and discoverable. `dataset` in the result (shape/fingerprint, if the script reported
    it) is included verbatim."""
    provenance = exp.get("provenance") or {}
    result = exp.get("result") or {}
    plan = provenance.get("dataset_plan") or "Synthesized in the experiment script (seeded)."
    direction = f"T{claim_id}" if claim_id is not None else "(no direction)"
    ds = result.get("dataset") or result.get("datasets")
    ds_block = f"\n**Reported shape / fingerprint:**\n```json\n{json.dumps(ds, indent=2)[:1200]}\n```\n" if ds else ""
    return (
        f"## Dataset for experiment {experiment_id} (direction {direction})\n\n"
        f"**How it was assembled:** {plan}\n\n"
        f"**Reproducibility:** regenerate by running the experiment's recorded code at "
        f"{_provenance_line(provenance)} — the data is produced deterministically from the "
        f"seed inside the pinned image, so the same bytes recur.{ds_block}\n"
    )
