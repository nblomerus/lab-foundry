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
from library.corpus.tools import corpus_search
from library.graph.field_model import read_field_brief
from library.graph.tools import _get_driver

log = logging.getLogger(__name__)

# Lab policy: ONLY DeepSeek (cloud) or local Ollama — no Gemini/Groq/OpenAI/etc.
ARIADNE_MODEL = os.environ.get("ARIADNE_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))

# The lab's compute envelope — so Ariadne frames directions that actually FIT the hardware
# instead of chasing data-centre problems. Overridable by env (ARIADNE_LAB_CONSTRAINTS).
LAB_CONSTRAINTS = os.environ.get("ARIADNE_LAB_CONSTRAINTS") or (
    "This is a SMALL autonomous lab, NOT a data centre. Compute envelope:\n"
    "- LLM inference via DeepSeek (cloud API) + local Ollama models up to ~32B (a single modest GPU; >14B is slow).\n"
    "- Embeddings + hybrid retrieval over a ~46k-document certified corpus.\n"
    "- Light CPU / single-GPU analysis and small, reproducible experiments.\n"
    "NOT available: large-scale training or fine-tuning, multi-GPU / data-centre compute, "
    "pretraining foundation models, or huge proprietary datasets.\n"
    "So FAVOUR: inference-time methods, prompting / agentic techniques, evaluation & benchmarking, "
    "analysis of existing literature and released models, methodology, and small reproducible studies. "
    "AVOID directions that REQUIRE training large models or data-centre-scale resources — score their "
    "feasibility and cost_efficiency LOW, or re-aim at a lighter, differentiated angle that fits this hardware."
)

_SYSTEM = """You are Ariadne, the Principal Investigator of an autonomous AI research lab.
You set strategy — you do NOT execute. Your job:
1. Frame the research MISSION from the seed problem.
2. Propose 3-5 candidate DIRECTIONS, each a falsifiable bet ("attack X via approach Y").
3. Ground each direction's NOVELTY in the PRIOR ART provided — name the SPECIFIC gap
   (what existing work misses, or where it's weak). Do not assert novelty; cite the gap.
   Populate grounded_in with the EXACT paper titles from the PRIOR ART shown that
   justify the gap. Use only titles that actually appear above; never invent one.
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
Be sharp, concrete, and honest about uncertainty. Output ONLY JSON."""

_SCHEMA_HINT = """Output JSON with exactly these keys:
{
 "mission_frame": str,
 "directions": [ { "title": str, "statement": str, "novelty_rationale": str,
   "grounded_in": [str],
   "scores": { "novelty": 1-5, "feasibility": 1-5, "evidence_availability": 1-5,
     "paper_potential": 1-5, "reviewer_interest": 1-5, "technical_depth": 1-5,
     "differentiation": 1-5, "cost_efficiency": 1-5, "lab_alignment": 1-5, "rationale": str },
   "claim_goals": [ { "expectation": str, "kill_condition": str,
     "novelty_target": str|null, "next_milestone": str|null, "priority_hint": "high|medium|low"|null } ],
   "kill_conditions": [str], "reviewer_risks": [str] } ],
 "novelty_risks": [str],
 "requests": [ { "paper": "exact title or arxiv id of a SPECIFIC missing paper",
   "arxiv_id": "YYMM.NNNNN"|null, "why": str } ],
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


async def recall_lessons(pool, *, limit: int = 10) -> str:
    """Ariadne's STANDING LESSONS — reflection's output fed back as deliberation input
    (the diagram's 'Past Lessons & Reflections'). Empty string if none / unavailable."""
    if pool is None:
        return ""
    try:
        rows = await pool.fetch(
            "SELECT lesson_text, applies_when, status FROM lessons "
            "WHERE applies_to_invocation IN ('ariadne.deliberate', 'ariadne.reflect') "
            "AND status IN ('active', 'probationary') "
            "ORDER BY (status = 'active') DESC, confidence DESC LIMIT $1",
            limit,
        )
    except Exception:  # noqa: BLE001 — lessons are best-effort context
        return ""
    if not rows:
        return ""
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
        a = await answer_question(question, k=8, state=state, asker="ariadne")
    except Exception as e:  # noqa: BLE001 — conversation is best-effort grounding
        log.warning("ariadne: Mimir conversation failed: %s", e)
        return "", []
    block = f"## Mimir's synthesis (multi-hop GraphRAG over the Library)\n{a.answer}"
    if a.gaps:
        block += (
            "\nUNDER-EXPLORED GAPS Mimir flags (prefer directions + evidence requests that "
            "fill these):\n" + "\n".join(f"- {g}" for g in a.gaps)
        )
    return block, a.gaps


async def recall_prior_art(seed: str, *, k: int = 8, pool=None, state=None) -> tuple[str, list[str]]:
    """Assemble grounding context: top retrieved passages + the FIELD MODEL (Domain-Expert
    landscape) + MIMIR'S SYNTHESIS (multi-hop GraphRAG conversation) + standing lessons.
    Each prior-art paper is tagged with its [arxiv:ID] (when known) so Ariadne can request a
    specific paper by id for a precise direct fetch. When `state` is given the Mimir conversation
    is emitted live. Returns (grounding_text, gaps) — the gaps drive gap-targeted directions."""
    chunks = await corpus_search(seed, k=k)
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


async def _deliberate(seed: str, agenda: str, prior_art: str, *, model: str) -> AriadneOutput:
    user = (
        f"# Seed problem\n{seed}\n\n"
        f"# Current agenda (the existing claims tree)\n{agenda}\n\n"
        f"# Grounding\n{prior_art}\n\n"
        f"# Lab capabilities & constraints (every direction MUST fit this hardware)\n{LAB_CONSTRAINTS}\n\n"
        f"# Task\nFrame the mission and propose the direction tree. Ground your strategy in MIMIR'S "
        f"SYNTHESIS above (the multi-hop GraphRAG read of the Library) and the FIELD MODEL: aim "
        f"directions at the UNDER-EXPLORED GAPS Mimir flags and EMERGING areas — do not restate "
        f"well-trodden work; treat SATURATED/DECLINING areas as saturation risks. Turn the gaps into "
        f"DIRECTIONS (they are research opportunities, not papers to fetch); use `requests` ONLY for a "
        f"specific paper (exact title or arxiv id) you think is genuinely missing — not for topics. "
        f"Every direction MUST be executable within the lab's constraints above — score "
        f"feasibility and cost_efficiency against them and do NOT propose data-centre-scale work. {_SCHEMA_HINT}"
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
    state, *, model: str = ARIADNE_MODEL, focus: str | None = None, emit_conversation: bool = False
) -> AriadneOutput:
    """Read seed problem + agenda, recall prior art, deliberate. WRITES NOTHING to the corpus.
    `focus` (e.g. an injected debug request) narrows the deliberation to a topic. When
    `emit_conversation` is True (the LIVE pacemaker path), the Ariadne↔Mimir GraphRAG exchange
    is emitted (mimir.ask/mimir.answered) so the floorplan shows it; the read-only firstlight
    dry-run leaves it False so nothing is written."""
    cs = await state.get_company_state()
    seed = cs.problem_statement
    if focus:
        seed = f"{seed}\n\nFOCUS THIS DELIBERATION ON: {focus}"

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

    # First-party experiment results on the active directions — so Ariadne re-frames over what the
    # lab actually RAN (numbers + the researcher's narrative note), not just confidence deltas.
    try:
        notes = await state.get_recent_experiment_notes_for_claims([c.id for c in claims], limit=8)
        if notes:
            agenda += "\n\n## Experiment results so far (first-party — what the lab ran)\n" + "\n".join(
                f"- T{n['claim_id']}: {(n.get('researcher_notes') or n.get('interpretation') or '')[:240]}" for n in notes
            )
    except Exception:  # noqa: BLE001 — experiment context is best-effort
        pass

    prior_art, _gaps = await recall_prior_art(  # gaps are embedded in prior_art
        seed, pool=state.pool, state=(state if emit_conversation else None)
    )
    return await _deliberate(seed, agenda, prior_art, model=model)
