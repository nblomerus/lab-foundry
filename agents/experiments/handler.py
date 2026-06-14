"""
The experiment "design language" — the curator recipes + prompt builders for the lab's experiments.

This module owns the THREE experiment LLM steps as curator recipes (registered idempotently at import):
    experiments.design     — write a self-contained script for a hypothesis under test
    experiments.debug      — read failed code + the error and return corrected code (the QM's fixer)
    experiments.interpret  — read a completed run's result honestly

The HANDLERS that drive these moved under the researcher (migration 022): the direction's OWNER authors
the experiment (agents.researcher.experiment_design) and interprets its own result
(agents.researcher.experiment_interpret), in their own full-stack voice. The Quartermaster owns the
off-slot run→debug→rerun loop (agents.experiments.session, which calls the debug recipe). What stays
here is the shared prompt language both of them build on.
"""

from __future__ import annotations

import json
import logging
import os
import re

from agents.experiments import sandbox
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
    lp = llm.chat_logprobs(model, msgs, top_logprobs=5)  # per-token logprobs (OpenAI-compat)
Use it to MEASURE real model behaviour: sampling/decoding strategies, self-consistency (majority vote
over samples), prompt-format effects, embedding geometry. TOKEN LOGPROBS ARE AVAILABLE via
llm.chat_logprobs (returns [{{"token","logprob","top_logprobs":[...]}}]) — so confidence CALIBRATION,
ECE, and perplexity ARE computable from real model probabilities (not just answer-level vote-share).
Calls serialize with the lab's other GPU work — roughly seconds per 7B call, tens of seconds at 14B+.
Budget honestly: n_calls × per-call seconds must fit est_wall_clock_s (cap 1800); prefer ≤7B models and
a modest call count. Generate probe INPUTS in-code (arithmetic/logic problems with known answers are
fine); model OUTPUTS must come from llm.* calls, never be fabricated.
"""
    return (
        block,
        " (plus the mounted /opt/lab/llm.py helper described above)",
        "it needs to fine-tune or train pretrained weights, a model beyond the local Ollama zoo and the "
        "staged /models, network access, or an external dataset",
    )


def _datasets_block() -> str:
    """The design prompt's /data section, rendered from the benchmark pack's manifest
    (ops.build_benchmark_pack) at call time — absent pack, absent section. Tags each dataset
    with modality/task_type so the designer can match a REAL dataset to the hypothesis."""
    manifest = sandbox.read_manifest()
    if not manifest:
        return ""
    lines = "\n".join(
        f"- /data/{d['file']} — [{d.get('modality', '?')}/{d.get('task_type', '?')}] {d['n']} rows; "
        f"{d['task']}; fields: {d['fields']} ({d['license']})"
        for d in manifest
    )
    return f"""
## REAL datasets mounted READ-ONLY at /data (offline, license-clean) — PREFER THESE
{lines}
Load (tabular):  import json; rows = [json.loads(l) for l in open("/data/adult.jsonl")]  # each row: features + "label"
Load (text):     import json; rows = [json.loads(l) for l in open("/data/gsm8k_test.jsonl")]
PREFER a real dataset above whose [modality/task_type] matches the hypothesis (classical-ML
claims → a tabular set; LLM-behaviour claims → a text set). If the PI's proposal named a
dataset_plan, use it. Sample a SEEDED subset sized to your wall-clock budget and report which
dataset, slice size, seed + sha256 in the result's `dataset`.
"""


def _models_block() -> str:
    """The design/debug prompt's offline HF model-zoo section, rendered from the zoo manifest
    (ops.build_model_zoo) at call time — absent zoo (EXPERIMENT_MODELS_DIR), absent section. Mounted
    read-only at /models so cross-encoder / NLI / encoder experiments run offline."""
    d = os.environ.get("EXPERIMENT_MODELS_DIR", "")
    if not d:
        return ""
    try:
        with open(os.path.join(d, "manifest.json")) as f:
            zoo = json.load(f) or []
    except (OSError, ValueError):
        return ""
    if not zoo:
        return ""
    lines = "\n".join(f"- /models/{m['path']} — [{m.get('task', '?')}] {m.get('name', '')}" for m in zoo)
    return f"""
## Offline PRETRAINED models mounted READ-ONLY at /models (no network — load by LOCAL PATH only)
{lines}
Load (reranker):  from sentence_transformers import CrossEncoder; ce = CrossEncoder("/models/<path>")
Load (encoder):   from sentence_transformers import SentenceTransformer; m = SentenceTransformer("/models/<path>")
`transformers` / `sentence_transformers` ARE available for THESE staged models — load by the /models
path above, NEVER an arbitrary HF hub name (there is no network; HF_HUB_OFFLINE is set).
"""


def _denylist_block() -> str:
    """What is NOT importable + the substitution. A positive allowlist alone didn't stop the author
    reaching for transformers / requests (a live audit's #1 + network failures), so name them."""
    md = os.environ.get("EXPERIMENT_MODELS_DIR", "")
    zoo_on = bool(md) and os.path.isdir(md)
    hf = (
        "- transformers / sentence_transformers: ONLY for the pre-staged /models zoo above — load by a "
        "LOCAL /models PATH, never an arbitrary HF hub name (no network).\n"
        if zoo_on
        else "- transformers / sentence_transformers / datasets / tokenizers: NOT installed — use the "
        "/opt/lab/llm.py helper for model behaviour instead.\n"
    )
    return (
        "## NOT available — do NOT import these (the sandbox has NO network; importing them crashes the run)\n"
        "- requests / httpx / urllib.request / aiohttp / a raw network socket: for ANY model call use the "
        "mounted /opt/lab/llm.py helper — never a URL or socket.\n"
        f"{hf}"
        "- tensorflow / jax / keras / cv2 / PIL / nltk / spacy: not in the image.\n"
    )


async def _build_design(ctx: dict, state, memory) -> PromptLayer:
    hypothesis = ctx.get("hypothesis") or ""
    goal = ctx.get("goal") or ""
    claim_statement = ctx.get("claim_statement") or ""
    # The OWNING researcher authors this (migration 022) — their full-stack persona leads the prompt
    # so the experiment is written in their voice, not a generic experimentalist's.
    persona = ctx.get("researcher_persona") or ""
    persona_block = f"{persona}\n\n---\n\n" if persona else ""
    lab_constraints = ctx.get("lab_constraints") or _LAB_CONSTRAINTS
    prior_hypotheses = ctx.get("prior_hypotheses") or []
    proposal_hypotheses = ctx.get("proposal_hypotheses") or []
    require_real_data = ctx.get("require_real_data") or False
    llm_block, llm_import, cannot = _llm_endpoint_blocks()
    real_required_block = (
        "\n## ⚠ REAL-DATA CONFIRMATION REQUIRED\n"
        "Prior runs on this direction used synthetic/toy data — this run must CONFIRM the claim on a "
        "REAL dataset. You MUST load a real /data dataset (see the catalog below) whose modality/task "
        "matches the claim; synthesizing or using a sklearn-builtin toy set is NOT acceptable here. If "
        "NO listed real dataset fits the claim, set `infeasible` true with the reason naming the kind of "
        "dataset that's missing — do NOT fall back to synthetic data.\n"
        if require_real_data
        else ""
    )
    proposal_block = ""
    if proposal_hypotheses:
        hyp_lines = "\n".join(
            f"- {h.get('hid', 'H?')}: {h.get('statement', '')} "
            f"(metric: {h.get('metric', '')}; decision: {h.get('threshold', '')}; "
            f"data plan: {h.get('dataset_plan') or '(unspecified)'})"
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
    models_block = _models_block()
    data_hint = " (pick from the /data catalog above)" if data_block else ""
    prior_block = (
        "## Experiments ALREADY run on this direction — test a DISTINCT facet, do NOT repeat these\n"
        + "\n".join(f"- {h}" for h in prior_hypotheses)
        + "\nDesign the NEXT experiment in the series: vary a factor, ablate a component, change the "
        "dataset/model/metric, or probe a boundary the prior runs did not — so the accumulated runs "
        "build a richer picture of the direction, not a repeat.\n\n"
        if prior_hypotheses
        else ""
    )

    content = f"""{persona_block}## Direction under test
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
{real_required_block}{llm_block}{data_block}{models_block}
{_denylist_block()}
Write `code` as a COMPLETE, self-contained Python script:
- Import ONLY the preinstalled stack: numpy, scipy, pandas, scikit-learn, xgboost, statsmodels, torch{llm_import}
  (see the NOT-available list above — never reach for transformers/requests/urllib; there is no network).
- DATA — real first. NO network; file reads only from the cwd and the read-only /data mount.
  PREFER a REAL /data dataset whose [modality/task_type] matches the claim{data_hint}; if the PI's
  proposal named a `dataset_plan`, use that dataset. Synthesize / use a sklearn-builtin toy set
  (make_classification, load_digits, a random tensor) ONLY when no listed real dataset fits the
  claim's regime (e.g. a controlled known-ground-truth or optimisation-dynamics study) — and then
  you MUST say WHY in `synthesis_justification`. A real dataset that exercises the claim beats a
  synthetic proxy every time. State the exact source in `dataset_plan` (loader/path, slice, seed).
- Seed every RNG you touch (numpy, torch, python `random`) from `seed` so the run reproduces.
- Keep it within the wall-clock and memory budgets you estimate. Modest is better than ambitious —
  a clean signal on a toy problem beats a run that times out.
- In your result JSON, include a `dataset` object capturing what you built/used so it's reproducible
  and inspectable: {{"n_samples", "n_features" (or shape), "source" (the generator/loader call),
  "sha256" (hashlib.sha256 of the data bytes, e.g. of X.tobytes())}}.
- EMIT THE RESULT as the LAST thing on stdout. PREFER the lab helper (handles numpy/torch types):
      import sys; sys.path.insert(0, "/opt/lab"); import exp; exp.emit(result)
  (or `print(json.dumps(result))` if every value is a plain Python type). `result` is a dict of the
  numbers you want interpreted (metrics, deltas, counts, p-values, timings) PLUS the `dataset` object.
  Print nothing after it. If stdout has no parseable JSON object the run is scored a FAILURE, so make
  that result line unmissable — and never `print(numpy_value)` directly, use exp.emit.

Set `requires_gpu` true ONLY if the run genuinely needs the GPU. Cap `est_wall_clock_s` at 1800; note
each ATTEMPT is capped near half that, so size a single run to finish well inside the budget.
Return JSON conforming to ExperimentDesign.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# ── data-realism classification (static, no LLM) ──────────────────────────────────
# A run is REAL only if it loads a real dataset (a /data file or a real loader); BUILTIN if it
# uses a sklearn toy loader; SYNTHETIC if it fabricates inputs (make_*/np.random/torch.randn) or
# we can't tell (conservative default — never claim 'real' without evidence). Checked in priority
# order so a real-data run that also seeds an RNG still classifies as real.
_REAL_RE = re.compile(
    r"/data/|open\(\s*['\"]/data|read_csv\(\s*['\"]/data|read_json\(\s*['\"]/data|np\.loadtxt\(\s*['\"]/data"
    r"|fetch_california|fetch_covtype|fetch_openml|load_dataset\(",
    re.I,
)
_BUILTIN_RE = re.compile(
    r"load_digits|load_wine|load_iris|load_breast_cancer|load_diabetes|load_linnerud"
    r"|fetch_20newsgroups|fetch_olivetti|fetch_lfw|fetch_kddcup",
    re.I,
)
_SYNTH_RE = re.compile(
    r"make_classification|make_regression|make_blobs|make_moons|make_circles|make_friedman"
    r"|np\.random|numpy\.random|default_rng|torch\.randn|torch\.rand\b|random\.(uniform|normal|gauss|choice|randint)",
    re.I,
)


def _classify_realism(code: str, dataset_source: str = "") -> str:
    """Classify an experiment's data as 'real' | 'builtin' | 'synthetic' from its code + the
    script's self-reported dataset source. Real (a /data file or real loader) wins, then builtin
    (sklearn toy set), then synthetic; unknown provenance is treated as synthetic, not real."""
    blob = f"{code or ''}\n{dataset_source or ''}"
    if _REAL_RE.search(blob):
        return "real"
    if _BUILTIN_RE.search(blob):
        return "builtin"
    if _SYNTH_RE.search(blob):
        return "synthetic"
    return "synthetic"


def _plan_wanted_real(dataset_plan: str, manifest_names: list[str]) -> bool:
    """Did the PI/designer's stated plan intend a REAL dataset? True if it references /data or names
    a real dataset from the catalog — used to flag a plan-vs-actual realism MISMATCH."""
    p = (dataset_plan or "").lower()
    if "/data/" in p:
        return True
    return any(n.lower() in p for n in manifest_names if n)


async def _build_interpret(ctx: dict, state, memory) -> PromptLayer:
    kind = ctx.get("kind") or "code"
    params = ctx.get("params") or {}
    result = ctx.get("result")
    hypothesis = ctx.get("hypothesis") or ""
    claim_statement = ctx.get("claim_statement") or ""
    realism = ctx.get("data_realism") or "synthetic"
    realism_mismatch = ctx.get("realism_mismatch") or False
    realism_note = {
        "real": "This run used a REAL dataset — its numbers can bear on a real-world claim.",
        "builtin": "This run used a sklearn BUILTIN toy dataset — suggestive, not a real-world result; calibrate down.",
        "synthetic": "This run used SYNTHETIC data — it CANNOT settle a real-world claim. Treat as a pilot: "
        "lean inconclusive unless the claim is explicitly about controlled/known-ground-truth behaviour.",
    }[realism]
    if realism_mismatch:
        realism_note += " ⚠ The plan named a REAL dataset but the run did NOT use one — flag this in the summary."
    persona = ctx.get("researcher_persona") or ""
    persona_block = f"{persona}\n\n---\n\n" if persona else ""

    content = f"""{persona_block}## Direction under test
{claim_statement or "(no direction statement)"}

## Hypothesis the experiment tested
{hypothesis or "(none recorded)"}

## Data realism of this run: {realism.upper()}
{realism_note}

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
    # The debugger gets the SAME environment context the designer has — without it, it was blind to the
    # libs/helper/manifest and oscillated (transformers→noJSON→transformers; /data path hallucinations).
    llm_block, _llm_import, _cannot = _llm_endpoint_blocks()
    data_block = _datasets_block()
    models_block = _models_block()

    content = f"""## Debugging an experiment (attempt {iteration})
You wrote a Python experiment to test a hypothesis; it FAILED to produce a usable result.
Read your own code and the failure, find the bug, and return CORRECTED code.

## Direction under test
{claim_statement or "(no direction statement)"}

## Hypothesis
{hypothesis or "(none stated)"}

## Lab compute envelope (the fix MUST still fit this)
{lab_constraints}
{_denylist_block()}{llm_block}{data_block}{models_block}
## The code that failed
```python
{code[:8000]}
```

## What went wrong (stderr / traceback / reason)
```
{error[:4000]}
```

---

Diagnose the actual failure and FIX it — use the environment described ABOVE, don't guess. Common fixes:
- ImportError / ModuleNotFound → the lib isn't installed (see the NOT-available list). Use only the
  preinstalled stack; for model behaviour use the /opt/lab/llm.py helper, NOT transformers/requests.
  Do NOT re-add the same missing import you just failed on.
- Network error (ConnectionRefused/urlopen) → there is NO network; replace the call with the
  /opt/lab/llm.py helper or remove it.
- FileNotFoundError on /data → the pack is FLAT; use an EXACT filename from the /data catalog above.
- Timeout (the run was killed) → make it cheaper: fewer samples/iterations, a smaller model.
- "not JSON serializable" / no result → emit with the helper: `import sys; sys.path.insert(0,"/opt/lab");
  import exp; exp.emit(result)` (coerces numpy/torch); ensure it is the LAST stdout line.
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
