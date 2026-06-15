"""
Ariadne's shadow-mode deliberation.

Given the lab's seed problem, she READS the trusted substrate — hybrid retrieval
(library.corpus.corpus_search) for prior art, and the context graph (the METHOD/TASK/
DATASET landscape we extracted) for "what is known / saturated" — and produces a
grounded direction tree (agents.ariadne.schemas.AriadneOutput). She WRITES NOTHING.

This is the framing + decompose core of the research-PI loop (assess_portfolio →
decompose). set_expectations is folded into each direction's claim_goals; decide_actions
/ reflect become meaningful once hypotheses have results, so they're stubs for now.

The LLM runs through the shared DeepSeek→local chain (agents.llm; lab policy: DeepSeek
cloud or local Ollama only), the primary model overridable by ARIADNE_MODEL. Novelty must
be grounded in the prior art shown — the whole point of building the substrate first.
"""

from __future__ import annotations

import json
import logging
import os
import re

from agents.ariadne.schemas import AriadneOutput
from agents.llm import _chain_complete, _strip_fences
from agents.mimir.ask import answer_question
from agents.researcher.runnable import affordance_brief
from library.corpus.tools import corpus_search
from library.graph.field_model import read_field_brief
from library.graph.tools import _get_driver

log = logging.getLogger(__name__)

# Lab policy: ONLY DeepSeek (cloud) or local Ollama — no Gemini/Groq/OpenAI/etc.
ARIADNE_MODEL = os.environ.get("ARIADNE_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))

# The lab's compute envelope — so Ariadne frames directions that actually FIT the hardware
# instead of chasing data-centre problems. Overridable by env (ARIADNE_LAB_CONSTRAINTS).
# The sandbox's model access is CONDITIONAL: with the inference broker up
# (EXPERIMENT_LLM_BROKER), local-model behaviour is genuinely testable; without it, any
# pretrained-model direction would force the designer to simulate — so it's forbidden.
_SANDBOX_LLM_ON = os.environ.get("EXPERIMENT_LLM_BROKER", "").lower() in {"on", "1", "true"}
_SANDBOX_LLM_MODELS = os.environ.get(
    "EXPERIMENT_LLM_MODELS",
    "mistral:7b-instruct-q4_K_M, qwen2.5:14b-instruct-q4_K_M, qwen2.5-coder:7b, nomic-embed-text",
)
# The offline HF model zoo (ops.build_model_zoo) — when staged, cross-encoder/NLI/encoder experiments
# become testable too, so advertise them to Ariadne.
_SANDBOX_MODELS_DIR = os.environ.get("EXPERIMENT_MODELS_DIR", "")
_ZOO_ON = bool(_SANDBOX_MODELS_DIR) and os.path.isdir(_SANDBOX_MODELS_DIR)
_ZOO_CLAUSE = (
    "A small OFFLINE pretrained-model zoo is mounted at /models (cross-encoder reranker, NLI, sentence "
    "encoder), so RETRIEVAL-RERANKING, NLI, and embedding experiments with real pretrained encoders are "
    "fair game (loaded by local path). "
    if _ZOO_ON
    else ""
)
_SANDBOX_MODEL_CLAUSE = (
    (
        "- The sandbox HAS a brokered LOCAL-MODEL endpoint: inference-time behaviour of these local "
        f"models IS testable — {_SANDBOX_LLM_MODELS} (≤7B fast, 14B moderate, 27B+ slow). Directions "
        "about sampling/decoding, self-consistency, prompt-format effects, or embedding geometry OF "
        "THESE MODELS are fair game. TOKEN LOGPROBS are exposed, so confidence CALIBRATION / ECE / "
        "perplexity on real model probabilities are testable too. "
        f"{_ZOO_CLAUSE}"
        "Probe inputs come from the mounted benchmark pack or are generated in-code. Still NOT testable: "
        "fine-tuning or training pretrained weights, models beyond the local Ollama zoo and the staged "
        "/models, web-scale benchmarks, multi-GPU / distributed training, or internet-fetched datasets.\n"
    )
    if _SANDBOX_LLM_ON
    else (
        "DO NOT propose directions whose decisive test needs a PRETRAINED model's behaviour — sampling/"
        "decoding strategies on 7B+ LLMs, quantization of pretrained nets, prompting or agentic "
        "scaffolding, LoRA fine-tunes, web-scale retrieval benchmarks. The sandbox cannot run them; "
        "the lab would be forced to simulate the outcome, which is fabricated evidence. If such a "
        "direction is irresistible, score feasibility 1 and expect it to be held.\n"
    )
)
LAB_CONSTRAINTS = os.environ.get("ARIADNE_LAB_CONSTRAINTS") or (
    "This is a SMALL autonomous lab, NOT a data centre. Compute envelope:\n"
    "- LLM inference (DeepSeek cloud + local Ollama) serves the lab's AGENTS — reading, reasoning, "
    "writing. The experiment sandbox reaches models ONLY as stated below.\n"
    "- EXPERIMENTS run in an OFFLINE sandbox: no network. Available stack: "
    "numpy / scipy / pandas / scikit-learn / xgboost / statsmodels / torch "
    "(CPU + a single modest GPU); classical models are built and trained FROM SCRATCH inside the run.\n"
    "- DATA — REAL FIRST. The sandbox mounts a read-only OFFLINE pack of REAL, license-clean datasets "
    "at /data: REAL TABULAR sets for classical ML (adult income, wine-quality, california-housing, "
    "forest-covertype) AND text/LLM benchmarks (GSM8K, TruthfulQA, BoolQ, HumanEval, MMLU, ARC, "
    "HellaSwag). PREFER grounding a direction's decisive test in one of these real datasets — name it "
    "in each hypothesis's dataset_plan. Synthetic / built-in toy data is a JUSTIFIED FALLBACK for "
    "controlled known-ground-truth or optimisation-dynamics studies, NOT the default. A direction "
    "testable on a REAL dataset is far stronger than one that can only be probed synthetically.\n"
    f"{_SANDBOX_MODEL_CLAUSE}"
    "- Embeddings + hybrid retrieval over a certified corpus (for LITERATURE grounding, not experiments).\n"
    "FAVOUR directions a sandbox experiment can SETTLE TODAY on a REAL /data dataset — a falsifiable "
    "claim with a measurable threshold, decided by code that outputs a metric: classical ML (GPs, "
    "kernels, SVMs, XGBoost, calibration, uncertainty) on the real tabular sets, small FROM-SCRATCH "
    "torch models (optimization dynamics, architecture ablations, generalization), and algorithmic / "
    "statistical claims (sampling, estimators, bandits, retrieval-scoring math).\n"
    "DO NOT frame META directions about the lab's OWN machinery — hypothesis-generation, "
    "retrieval-augmented-LLM 'methodology', 'evidence packs', or literature surveys. The lab STUDIES "
    "ml/ai methods; it does NOT study how it does research. 'Analyse the literature on X' is NOT a "
    "direction; 'method X beats baseline Y on metric Z by >=delta under setting S' IS.\n"
    "AVOID directions that REQUIRE training large models or data-centre-scale resources — score their "
    "feasibility and cost_efficiency LOW, or re-aim at a lighter, differentiated angle that fits this hardware."
)

_SYSTEM = """You are Ariadne, the Principal Investigator of an autonomous AI research lab.
You set strategy — you do NOT execute. Each direction must be a PAPER-SHAPED CONTRIBUTION
that clears THREE bars together: it MATTERS (a clear answer changes a real decision), it is
NOVEL (a new finding/method, not a confirmation), and it is PUBLISHABLE. Your job:
1. Frame the research MISSION from the seed problem and its stance.
2. For EACH direction, LEAD WITH THE STAKES: in one sentence, the real build/deploy DECISION a
   clear answer changes, WHO acts on it (a named practitioner / system-builder), and what it
   settles or saves — put this in `stakes`. A direction no one would change behaviour on, however
   novel, is NOT worth running. Then state it as a falsifiable bet ("attack X via approach Y").
3. Ground each direction's NOVELTY in the PRIOR ART provided — name the SPECIFIC gap
   (what existing work misses, or where it's weak). A gap is NECESSARY but NOT SUFFICIENT:
   pursue it ONLY when filling it would also change how practitioners build systems. Do not
   assert novelty; cite the gap. Populate grounded_in with EXACT paper titles from the PRIOR ART
   shown that justify the gap. Use only titles that actually appear above; never invent one.
4. For each direction give claim_goals (expectation, kill_condition, novelty_target,
   next_milestone, priority_hint), kill_conditions, and reviewer_risks (weak evaluation,
   weak baselines, LLM-judge bias, reproducibility, novelty concerns).
You never write execution tasks and never scout directly. Turn under-explored GAPS into
DIRECTIONS — they are research opportunities to pursue, not papers to fetch. Use 'requests'
ONLY for a SPECIFIC paper (its exact title, or an arxiv id) you believe is genuinely missing
(a foundational older work, or a cross-domain paper the corpus likely lacks). Never request a
broad topic — the corpus already covers topics comprehensively, so a topic just comes back
'already have'. Prior-art papers in the GROUNDING are tagged with their [arxiv:ID] — when you
request one of them, copy that id into 'arxiv_id' for a precise, direct fetch.
EXPERIMENTS RUN ON STAGED DATA: a direction is only runnable if the dataset/model it needs is in
the live "What the lab can ACTUALLY RUN now" list. If a HIGH-value direction needs a dataset the lab
does NOT have, do NOT silently score feasibility low and drop it — add a `data_requests` entry naming
the SPECIFIC, realistically-downloadable dataset (an exact HuggingFace / OpenML / UCI id); the lab
records that demand for Mimir/ops to fulfil (the direction can't run until the dataset is staged, so
PREFER directions runnable on data already staged). Only request data that genuinely exists and is
downloadable; capability gaps a dataset can't fix (model internals/gradients, an open-domain retrieval
corpus, a model not in the zoo) are NOT data_requests — avoid proposing those.
Be sharp, concrete, and honest about uncertainty. Output ONLY JSON."""

_SCHEMA_HINT = """Output JSON with exactly these keys:
{
 "mission_frame": str,
 "directions": [ { "title": str, "statement": str,
   "stakes": "the real DECISION a clear answer changes + WHO acts on it (named actor) + what it settles",
   "novelty_rationale": str,
   "grounded_in": [str],
   "scores": { "novelty": 1-5, "impact": 1-5, "feasibility": 1-5, "evidence_availability": 1-5,
     "paper_potential": 1-5, "reviewer_interest": 1-5, "technical_depth": 1-5,
     "differentiation": 1-5, "cost_efficiency": 1-5, "lab_alignment": 1-5, "rationale": str },
   "claim_goals": [ { "expectation": str, "kill_condition": str,
     "novelty_target": str|null, "next_milestone": str|null, "priority_hint": "high|medium|low"|null } ],
   "kill_conditions": [str], "reviewer_risks": [str] } ],
 "novelty_risks": [str],
 "requests": [ { "paper": "exact title or arxiv id of a SPECIFIC missing paper",
   "arxiv_id": "YYMM.NNNNN"|null, "why": str } ],
 "data_requests": [ { "dataset": "exact HF/OpenML/UCI id of a dataset the lab lacks but needs",
   "source": "huggingface|openml|uci"|null, "modality": str|null, "task_type": str|null, "why": str } ],
 "reflection": str
}
Every direction MUST include all nine integer scores (1=poor … 5=excellent); score novelty
and differentiation against the FIELD MODEL (EMERGING/under-served high, SATURATED low)."""


async def _top_concepts(label: str, rel: str, limit: int = 18) -> list[tuple[str, int]]:
    """The corpus's prominent concepts from the context graph — Ariadne's read on
    'what is known / saturated' (e.g. top METHODs by paper count)."""
    try:
        driver = await _get_driver()
        async with driver.session() as session:
            res = await session.run(
                f"MATCH (n:{label})<-[:{rel}]-(p:Paper) "
                f"RETURN n.name AS name, count(DISTINCT p) AS papers "
                f"ORDER BY papers DESC LIMIT $limit",
                limit=limit,
            )
            return [(r["name"], r["papers"]) async for r in res]
    except Exception as e:  # noqa: BLE001 — graph is best-effort context
        log.warning("ariadne: concept landscape query failed: %s", e)
        return []


def _lesson_when(v) -> str:
    """The bare condition from a lesson's applies_when jsonb — tolerant of {'when': cond},
    raw JSON text (no codec registered), or a bare string."""
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return v
    if isinstance(v, dict):
        return v.get("when") or ""
    return str(v)


# The lesson ids the LAST recall injected — so the handler can record lesson_applications
# for the run that consumed them (the Curator/Router path records its own; Ariadne bypasses
# it, which left her lessons unjudgeable: 0 promotions ever). Single-flight is safe: the
# pacemaker emits at most one deliberate/reflect at a time and never stacks pending ones.
LAST_RECALLED_LESSON_IDS: list[int] = []


async def recall_lessons(pool, *, limit: int = 10) -> str:
    """Ariadne's STANDING LESSONS — reflection's output fed back as deliberation input
    (the diagram's 'Past Lessons & Reflections'). Empty string if none / unavailable."""
    LAST_RECALLED_LESSON_IDS.clear()
    if pool is None:
        return ""
    try:
        rows = await pool.fetch(
            "SELECT l.id, l.lesson_text, l.applies_when, l.status "
            "FROM lessons l LEFT JOIN LATERAL ("
            "  SELECT count(*) AS supp, max(created_at) AS last_app FROM lesson_applications la "
            "  WHERE la.lesson_id = l.id AND la.outcome = 'supportive'"
            ") s ON true "
            "WHERE l.applies_to_invocation IN ('ariadne.deliberate', 'ariadne.reflect') "
            "AND l.status IN ('active', 'probationary') "
            # active first, then most- / most-recently-reinforced, then confidence — so promoted and
            # re-derived lessons win the limited recall window over one-off probationary noise.
            "ORDER BY (l.status = 'active') DESC, COALESCE(s.supp, 0) DESC, "
            "s.last_app DESC NULLS LAST, l.confidence DESC LIMIT $1",
            limit,
        )
    except Exception:  # noqa: BLE001 — lessons are best-effort context
        return ""
    if not rows:
        return ""
    LAST_RECALLED_LESSON_IDS.extend(r["id"] for r in rows)
    items = []
    for r in rows:
        cond = _lesson_when(r["applies_when"])
        when = f" (when: {cond})" if cond else ""
        items.append(f"- [{r['status']}] {r['lesson_text']}{when}")
    return "## Standing lessons (carry these forward from prior reflection)\n" + "\n".join(items)


_ARXIV_RE = re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})")


def _arxiv_tag(source_url: str | None) -> str:
    """`' [arxiv:2406.12345]'` for an arXiv source_url, else ''. Lets Ariadne copy a precise id
    into a request for a direct fetch instead of a fuzzy title query."""
    m = _ARXIV_RE.search(source_url or "")
    return f" [arxiv:{m.group(1)}]" if m else ""


_GAP_COVERAGE_SIM = 0.6  # top-hit cosine sim above which a flagged 'gap' is plausibly already covered
_GAP_COVERAGE_MIN_DOCS = 3


async def _annotate_gaps(gaps: list[str]) -> list[str]:
    """Cross-check each Mimir-flagged gap against a direct EXTERNAL hybrid search and ANNOTATE (never
    drop) the ones the corpus visibly already covers. Mimir's gap signal is graph-neighborhood coverage
    of the retrieved set, which can undercount a well-covered topic (fragmented concept nodes / thin
    retrieval → the false 'Gaussian process is absent' failure observed live); surfacing the real
    external coverage lets deliberation reject a false gap instead of chasing it. A genuine gap has a
    LOW top-hit similarity (its nearest neighbours are only loosely related) and passes through
    unannotated, so this never suppresses real gaps. Best-effort."""
    out: list[str] = []
    for g in gaps:
        try:
            hits = await corpus_search(g, k=8, exclude_lab=True)
            ndocs = len({h.document_id for h in hits})
            top = max((h.sim for h in hits), default=0.0)
            if ndocs >= _GAP_COVERAGE_MIN_DOCS and top >= _GAP_COVERAGE_SIM:
                out.append(
                    f"{g}  [corpus check: ~{ndocs} on-topic papers already exist (top match {int(top * 100)}%) "
                    "— confirm this is genuinely under-explored, not a retrieval blind spot, before targeting]"
                )
            else:
                out.append(g)
        except Exception:  # noqa: BLE001 — corroboration is best-effort; keep the gap as-is
            out.append(g)
    return out


async def _mimir_brief(seed: str, pool, *, state=None) -> tuple[str, list[str]]:
    """CONVERSE with Mimir: ask a strategic, multi-hop question about the mission's landscape and
    get a SYNTHESIZED answer + the under-explored GAPS to target. This is Ariadne thinking WITH
    Mimir (GraphRAG), not just reading passages. When `state` is given the conversation is emitted
    (mimir.ask / mimir.answered) so the floorplan shows it live. Best-effort. Returns (block, gaps)."""
    if pool is None:
        return "", []
    try:
        rows = await pool.fetch(
            "SELECT concept_name FROM field_model WHERE trend_state IN ('emerging','hot') "
            "ORDER BY (trend_state = 'emerging') DESC, total_papers DESC LIMIT 6"
        )
        anchors = ", ".join(r["concept_name"] for r in rows)
    except Exception:  # noqa: BLE001
        anchors = ""
    question = (
        f"For the research mission: {seed[:400]}. "
        f"{('Focus on active/emerging areas: ' + anchors + '. ') if anchors else ''}"
        "What methods, tasks, and datasets exist and how do they connect? What is well-covered "
        "versus thin, and what are the most promising UNDER-EXPLORED gaps to investigate within a "
        "small inference-only lab?"
    )
    try:
        a = await answer_question(question, k=8, state=state, asker="ariadne", exclude_lab=True)
    except Exception as e:  # noqa: BLE001 — conversation is best-effort grounding
        log.warning("ariadne: Mimir conversation failed: %s", e)
        return "", []
    block = f"## Mimir's synthesis (multi-hop GraphRAG over the Library)\n{a.answer}"
    gaps = await _annotate_gaps(a.gaps) if a.gaps else []
    if gaps:
        block += (
            "\nUNDER-EXPLORED GAPS Mimir flags (prefer directions + evidence requests that "
            "fill these):\n" + "\n".join(f"- {g}" for g in gaps)
        )
    return block, gaps


async def recall_prior_art(seed: str, *, k: int = 8, pool=None, state=None) -> tuple[str, list[str]]:
    """Assemble grounding context: top retrieved passages + the FIELD MODEL (Domain-Expert
    landscape) + MIMIR'S SYNTHESIS (multi-hop GraphRAG conversation) + standing lessons.
    Each prior-art paper is tagged with its [arxiv:ID] (when known) so Ariadne can request a
    specific paper by id for a precise direct fetch. When `state` is given the Mimir conversation
    is emitted live. Returns (grounding_text, gaps) — the gaps drive gap-targeted directions."""
    chunks = await corpus_search(seed, k=k, exclude_lab=True)
    passages = (
        "\n".join(
            f"[{c.trust_tier}] {(c.title or 'untitled')[:90]}{_arxiv_tag(c.source_url)} — {c.text[:320].strip()}"
            for c in chunks
        )
        or "(no passages retrieved)"
    )

    landscape = await read_field_brief(pool) if pool is not None else ""
    if not landscape:
        methods = await _top_concepts("METHOD", "USES")
        tasks = await _top_concepts("TASK", "ADDRESSES")
        datasets = await _top_concepts("DATASET", "EVALUATED_ON")

        def _fmt(items):
            return ", ".join(f"{n} ({c})" for n, c in items) or "(none extracted yet)"

        landscape = (
            "## Corpus concept landscape (context graph; name(paper_count) — what is well-trodden)\n"
            f"METHODS: {_fmt(methods)}\nTASKS: {_fmt(tasks)}\nDATASETS: {_fmt(datasets)}"
        )

    mimir_block, gaps = await _mimir_brief(seed, pool, state=state)
    lessons = await recall_lessons(pool)
    parts = [f"## Retrieved prior art (hybrid retrieval over the certified corpus)\n{passages}", landscape]
    if mimir_block:
        parts.append(mimir_block)
    if lessons:
        parts.append(lessons)
    return "\n\n".join(parts), gaps


async def _deliberate(
    seed: str, agenda: str, prior_art: str, *, model: str, stance: str = "", success: str = ""
) -> AriadneOutput:
    bars = ""
    if stance:
        bars += f"# Research stance — the bar EVERY direction must clear\n{stance}\n\n"
    if success:
        bars += f"# What success looks like\n{success}\n\n"
    # Live run-capability (the SAME /data + /models manifest the experiment sandbox mounts) — so she
    # proposes directions the lab can actually RUN, and names a data_request when a dataset is missing,
    # instead of the stale prose constraints alone. Self-suppressing when nothing is staged.
    brief = affordance_brief()
    runnable_block = (
        f"# What the lab can ACTUALLY RUN now (live /data + /models manifest — propose runnable "
        f"directions; if a needed dataset is NOT here, add a data_requests entry)\n{brief}\n\n"
        if brief
        else ""
    )
    user = (
        f"# Seed problem\n{seed}\n\n"
        f"{bars}"
        f"# Current agenda (the existing claims tree)\n{agenda}\n\n"
        f"# Grounding\n{prior_art}\n\n"
        f"{runnable_block}"
        f"# Lab capabilities & constraints (every direction MUST fit this hardware)\n{LAB_CONSTRAINTS}\n\n"
        f"# Task\nFrame the mission and propose the direction tree. Each direction is a PAPER-SHAPED "
        f"CONTRIBUTION: 'We show that [novel finding] on [task], which means [a named practitioner should "
        f"do X differently].' It must clear THREE bars together — (1) IMPACT: a clear answer changes a real "
        f"build/deploy DECISION someone faces (state the `stakes`: the decision + WHO acts on it); (2) NOVELTY: "
        f"a new finding/method, NOT a confirmation or survey; (3) PUBLISHABLE: a contribution worth a paper. "
        f"Use Mimir's SYNTHESIS + the FIELD MODEL to find where this is possible, but treat 'it's an "
        f"under-explored GAP' as NECESSARY, NOT SUFFICIENT — a gap nobody would act on the answer to is NOT "
        f"worth running, however novel. Each direction MUST be a SUBSTANTIVE, falsifiable ML/AI claim settled "
        f"by a REAL experiment that outputs a metric on this hardware — NOT meta-methodology about the lab's "
        f"own pipeline and NOT a literature survey. Use `requests` ONLY for a specific missing paper (exact "
        f"title or arxiv id), not topics. Score feasibility/cost_efficiency against the hardware; do NOT "
        f"propose data-centre-scale work. {_SCHEMA_HINT}"
    )
    content = await _chain_complete(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        temperature=0.4,
        invocation_type="ariadne.deliberate",
        step_name="deliberate",
        primary_model=model,
    )
    return AriadneOutput.model_validate_json(_strip_fences(content))


async def run_shadow(
    state,
    *,
    model: str = ARIADNE_MODEL,
    focus: str | None = None,
    feedback: str | None = None,
    emit_conversation: bool = False,
) -> AriadneOutput:
    """Read seed problem + agenda, recall prior art, deliberate. WRITES NOTHING to the corpus.
    `focus` (e.g. an injected debug request) narrows the deliberation to a topic. `feedback`
    carries the grader's corrective notes from a FAILED previous attempt (the handler's
    one-shot retry) so the model fixes the actual defects instead of re-rolling blind. When
    `emit_conversation` is True (the LIVE pacemaker path), the Ariadne↔Mimir GraphRAG exchange
    is emitted (mimir.ask/mimir.answered) so the floorplan shows it; the read-only firstlight
    dry-run leaves it False so nothing is written."""
    cs = await state.get_company_state()
    seed = cs.problem_statement
    if focus:
        seed = f"{seed}\n\nFOCUS THIS DELIBERATION ON: {focus}"
    if feedback:
        seed = f"{seed}\n\n## VALIDATION FEEDBACK — your previous agenda FAILED these checks; fix ALL of them\n{feedback}"

    # The agenda tree (claim_kind='mission'/'direction'/'hypothesis'). Empty on a fresh
    # lab — shadow Ariadne frames from scratch. Kept defensive: any read failure → empty.
    claims = []
    try:
        claims = await state.get_active_claims()
        agenda = (
            "\n".join(f"- [{getattr(c, 'claim_kind', 'hypothesis')}] {c.statement}" for c in claims)
            or "(empty — frame from scratch)"
        )
    except Exception:  # noqa: BLE001
        agenda = "(empty — frame from scratch)"

    # First-party EXECUTION LEDGER — every direction the lab has actually RUN (active OR already
    # killed), with done/failed counts + the latest researcher headline + why a killed one died.
    # Unions active-unworked with worked-invalidated (status-agnostic) so she stops re-attacking
    # ground the lab already ran and killed — the prior per-active-claim read was empty live because
    # the active directions had zero experiments while the worked ones were already invalidated.
    try:
        ledger = await state.get_direction_execution_digest([c.id for c in claims], limit=12)
        if ledger:
            agenda += (
                "\n\n## Execution ledger (first-party — what the lab has RUN; do NOT re-attack "
                "worked-and-killed ground)\n"
                + "\n".join(
                    f"- T{r['claim_id']} [{r['status']}, {r['done']} done / {r['failed']} failed]"
                    f"{(' KILLED: ' + (r['invalidation_reason'] or '')[:80]) if r['status'] == 'invalidated' else ''}"
                    f"{(' — ' + r['headline'][:160]) if r.get('headline') else ''}"
                    for r in ledger
                )
            )
    except Exception:  # noqa: BLE001 — execution ledger is best-effort
        pass

    # Paper-shaped FINDINGS the lab has ESTABLISHED — its terminal conclusions (supported/refuted +
    # so-what). Read GLOBALLY, not per-active-claim: a finding survives a re-frame but its direction
    # bond goes inactive, so a per-claim read would show nothing right after she re-frames. This is the
    # durable memory channel — round N+1 builds BEYOND what the lab concluded instead of re-rolling.
    try:
        findings = await state.get_recent_findings(limit=8)
        if findings:
            agenda += (
                "\n\n## Findings the lab has ESTABLISHED (first-party — build BEYOND these, do not repeat)\n"
                + "\n".join(
                    f"- [{f['supported']} @{float(f['confidence'] or 0):.2f}] {f['headline']} "
                    f"— so what: {(f.get('so_what') or '')[:160]}"
                    for f in findings
                )
            )
    except Exception:  # noqa: BLE001 — findings context is best-effort
        pass

    # Directions the INDEPENDENT adjudicator recently HELD (prior-art overlap / re-tread) — so the
    # re-frame steers AWAY from near-duplicates instead of re-proposing them. Without this edge the
    # lab churns deliberate→hold→agenda-exhausted→deliberate, blind to why (observed live: all live
    # directions held, the arc never starts). Best-effort; the adjudicator side already filters too.
    try:
        held = await state.get_held_directions_with_rationale(limit=8)
        if held:
            agenda += (
                "\n\n## Directions the adjudicator just HELD (do NOT re-propose near-duplicates of these)\n"
                + "\n".join(f"- [held — {(h.get('rationale') or '')[:160]}] {h['statement']}" for h in held)
            )
    except Exception:  # noqa: BLE001 — held-context is best-effort
        pass

    prior_art, _gaps = await recall_prior_art(  # gaps are embedded in prior_art
        seed, pool=state.pool, state=(state if emit_conversation else None)
    )
    return await _deliberate(
        seed, agenda, prior_art, model=model, stance=cs.stance or "", success=cs.success_criterion or ""
    )
