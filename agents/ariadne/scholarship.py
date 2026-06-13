"""
Ariadne's SCHOLARSHIP — the PI's written work products in the traditional research arc.

Choosing the topic was always hers (deliberation). This module makes the next two PI
steps real documents instead of implicit state:

    ariadne.review  → a LITERATURE REVIEW for an approved direction — materializing the
                      prior-art grounding + Mimir conversation she already does, as a
                      citable document (citation-graded like deliberation: never invent
                      a title).
    ariadne.propose → a RESEARCH PROPOSAL — research questions from the review's gaps
                      and falsifiable hypotheses with measurable decision thresholds
                      (formalizing her claim_goals), plus the method plan the experiment
                      series will follow. The experiment designer consumes these
                      hypotheses, so the series tests HER plan, not ad-hoc restatements.

Both live in agents/ariadne/* — agent_of maps them to the `ariadne` mode dial; pausing
the PI pauses her scholarship. Documents persist in research_documents (one final per
direction+kind; rewrites supersede) and are ingested into the Library as first-party
`lab_scholarship` sources so Mimir carries the lab's own scholarship.

The third document — the ARTICLE — is deliberately NOT here: writing up the lab's
findings is the Synthesis agent's job (agents/synthesis/article.py).
"""

from __future__ import annotations

import json
import logging
import os

from pydantic import ValidationError

from agents.ariadne.grade import _resolves
from agents.ariadne.loop import LAB_CONSTRAINTS, _annotate_gaps
from agents.experiments import sandbox
from agents.llm import complete_validated
from agents.mimir.ask import answer_question
from agents.scholarship_schemas import LiteratureReview, ResearchProposal
from harness.agent_modes import get_agent_mode
from library.corpus.tools import corpus_search

log = logging.getLogger(__name__)

ARIADNE_MODEL = os.environ.get("ARIADNE_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))
CITATION_RESOLVE_MIN = 0.8  # the deliberation bar

# The lab's house templates for the written arc (agents/ariadne/templates/*.md) — the PI fills
# these section-by-section so every literature review / proposal has the same rigorous shape.
# Editable markdown: change the template, change the document structure. Cached at import.
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _template(name: str) -> str:
    try:
        with open(os.path.join(_TEMPLATE_DIR, f"{name}.md")) as f:
            return f.read()
    except OSError:
        log.warning("ariadne scholarship: template %s missing — proceeding without it", name)
        return ""


_LIT_REVIEW_TEMPLATE = _template("literature_review")
_PROPOSAL_TEMPLATE = _template("research_proposal")

_SYSTEM = (
    "You are Ariadne, the Principal Investigator of an autonomous AI research lab, writing "
    "your lab's research documents. You are rigorous and honest: every prior-work claim cites "
    "an EXACT title from the corpus excerpts provided (never invent one), and every hypothesis "
    "carries a measurable decision threshold the lab's experiments can actually settle. "
    "Output ONLY JSON."
)


async def _direction(state, claim_id: int) -> dict | None:
    async with state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.id, c.statement, c.status, "
            "(SELECT ds.rationale FROM direction_scores ds WHERE ds.claim_id = c.id LIMIT 1) AS stakes "
            "FROM claims c WHERE c.id = $1 AND c.claim_kind = 'direction'",
            claim_id,
        )
        if row is None:
            return None
        goals = await conn.fetch(
            "SELECT expectation, kill_condition FROM claim_goals WHERE claim_id = $1 ORDER BY id", claim_id
        )
    d = dict(row)
    d["goals"] = "\n".join(f"- expect: {g['expectation']} || kill: {g['kill_condition']}" for g in goals)
    return d


async def _prior_art(statement: str, k: int = 12) -> str:
    try:
        chunks = await corpus_search(statement, k=k, exclude_lab=True)
    except Exception:  # noqa: BLE001 — grounding is best-effort; the grader catches emptiness
        log.exception("ariadne scholarship: corpus_search failed")
        return "(corpus search unavailable)"
    seen: dict[str, str] = {}
    for c in chunks:
        if c.title and c.title not in seen:
            seen[c.title] = c.text[:400]
    return "\n".join(f"- “{t}”: {txt}" for t, txt in list(seen.items())[:12]) or "(no prior art retrieved)"


async def _mimir_brief(statement: str, state) -> str:
    try:
        a = await answer_question(
            f"For the research direction: {statement[:300]} — what is established in the literature, "
            "what methods/results are most relevant, and where are the genuine gaps?",
            k=8,
            state=state,
            asker="ariadne",
            exclude_lab=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ariadne scholarship: Mimir brief failed: %s", e)
        return ""
    block = f"## Mimir's synthesis (multi-hop GraphRAG over the Library)\n{a.answer}"
    if a.gaps:
        gaps = await _annotate_gaps(a.gaps)
        block += "\nGaps Mimir flags:\n" + "\n".join(f"- {g}" for g in gaps)
    return block


def _catalog_block() -> str:
    """The REAL-dataset catalog (the offline /data pack) rendered for the PI's proposal prompt, so
    each hypothesis's dataset_plan can name a real dataset that actually exists. Empty if no pack."""
    manifest = sandbox.read_manifest()
    if not manifest:
        return ""
    lines = "\n".join(
        f"- {d['name']} [{d.get('modality', '?')}/{d.get('task_type', '?')}] — {d['n']} rows; {d['task']}"
        for d in manifest
    )
    return (
        "# REAL datasets available offline at /data (the lab's experiment sandbox mounts these)\n"
        f"{lines}\n"
        "Each hypothesis's dataset_plan should, WHERE one of these fits the claim, NAME it "
        "(classical-ML/tabular claims → a tabular set; LLM-behaviour claims → a text set).\n\n"
    )


async def _grade_citations(citations: list[str]) -> tuple[float, list[str]]:
    resolved, unresolved = 0, []
    for c in citations:
        if await _resolves(c):
            resolved += 1
        else:
            unresolved.append(c)
    return (resolved / len(citations) if citations else 0.0), unresolved


async def persist_document(
    state, claim_id: int, kind: str, title: str, body_md: str, meta: dict, citations: list[str]
) -> int:
    """One FINAL document per (direction, kind): a rewrite supersedes the prior. Shared by
    the PI's documents here and Synthesis' article (agents/synthesis/article.py)."""
    async with state.pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE research_documents SET status = 'superseded' WHERE claim_id = $1 AND kind = $2 AND status = 'final'",
            claim_id,
            kind,
        )
        return await conn.fetchval(
            "INSERT INTO research_documents (claim_id, kind, title, body_md, meta, citations, status) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'final') RETURNING id",
            claim_id,
            kind,
            title,
            body_md,
            json.dumps(meta),
            json.dumps(citations),
        )


async def ingest_to_library(state, claim_id: int, kind: str, title: str, body_md: str, doc_id: int) -> None:
    """The lab's scholarship enters its own Library (first-party). Best-effort: an ingest
    failure must not lose the document row."""
    try:
        await state.emit_corpus_event(
            "source.discovered",
            target_type="source",
            target_id=doc_id,
            payload={
                "source": {
                    "kind": "note",
                    "source_kind": "lab_scholarship",
                    "canonical_key": f"scholarship:{kind}:claim:{claim_id}:doc:{doc_id}",
                    "title": title,
                    "why": f"first-party {kind.replace('_', ' ')} for direction #{claim_id}",
                },
                "content": body_md,
                "provenance": {"claim_id": claim_id, "research_document_id": doc_id, "kind": kind},
            },
            dedup_key=f"scholar-doc-{doc_id}",
        )
    except Exception:  # noqa: BLE001
        log.exception("ariadne scholarship: Library ingest emit failed for doc %s", doc_id)


async def handle_ariadne_review(event: dict, dispatcher) -> dict | None:
    """`ariadne.review` → the PI writes the literature review for an approved direction."""
    state = dispatcher.state
    claim_id = (event.get("payload") or {}).get("claim_id")
    if claim_id is None or await get_agent_mode(state.pool, "ariadne") not in {"advisory", "active"}:
        return {"skipped": True, "reason": "no claim_id or ariadne paused"}
    d = await _direction(state, claim_id)
    if d is None:
        return {"skipped": True, "reason": f"direction {claim_id} not found"}

    prior = await _prior_art(d["statement"])
    mimir = await _mimir_brief(d["statement"], state)
    user = (
        f"# Direction under study\n{d['statement']}\n\n"
        f"# Why it matters (stakes)\n{d.get('stakes') or '(not recorded)'}\n\n"
        f"# Prior art from the lab's corpus (cite by EXACT title; never invent)\n{prior}\n\n"
        f"{mimir}\n\n"
        "# Task\nWrite the LITERATURE REVIEW for this direction by FILLING OUT the lab's template "
        "below. `body_md` MUST follow the template's section structure (keep every `##` heading and "
        "the markdown tables); replace each [bracketed] placeholder with real content grounded in the "
        "prior art above — every prior-work claim cites an EXACT corpus title (never invent one). Use "
        "'none identified' where a section genuinely has nothing. Populate `citations` with the exact "
        "titles used. Output JSON for: title, body_md (the filled template), citations.\n\n"
        f"# TEMPLATE — fill this out\n{_LIT_REVIEW_TEMPLATE}"
    )
    try:
        review = await complete_validated(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            LiteratureReview,
            invocation_type="ariadne.review",
            step_name="ariadne.review",
            primary_model=ARIADNE_MODEL,
        )
    except ValidationError as e:
        log.warning("ariadne scholarship: review for #%s failed schema validation after retry: %s", claim_id, e)
        return {"claim_id": claim_id, "persisted": False, "reason": "schema_invalid"}

    frac, unresolved = await _grade_citations(review.citations)
    if frac < CITATION_RESOLVE_MIN:
        log.warning(
            "ariadne scholarship: review for #%s FAILED citation grading (%.0f%%, unresolved=%s)",
            claim_id,
            frac * 100,
            unresolved[:4],
        )
        return {"claim_id": claim_id, "persisted": False, "reason": "citations_unresolved", "resolved": frac}

    doc_id = await persist_document(
        state,
        claim_id,
        "lit_review",
        review.title,
        review.body_md,
        {"citations_resolved": round(frac, 2)},
        review.citations,
    )
    await ingest_to_library(state, claim_id, "lit_review", review.title, review.body_md, doc_id)
    log.info(
        "ariadne scholarship: literature review for #%s persisted (doc %s, %d citations)",
        claim_id,
        doc_id,
        len(review.citations),
    )
    return {"claim_id": claim_id, "document_id": doc_id, "kind": "lit_review", "persisted": True}


async def handle_ariadne_propose(event: dict, dispatcher) -> dict | None:
    """`ariadne.propose` → the PI writes the research proposal (RQs + hypotheses + method)."""
    state = dispatcher.state
    claim_id = (event.get("payload") or {}).get("claim_id")
    if claim_id is None or await get_agent_mode(state.pool, "ariadne") not in {"advisory", "active"}:
        return {"skipped": True, "reason": "no claim_id or ariadne paused"}
    d = await _direction(state, claim_id)
    if d is None:
        return {"skipped": True, "reason": f"direction {claim_id} not found"}
    review = await state.get_research_document(claim_id, "lit_review")
    if review is None:
        return {"skipped": True, "reason": "no literature review yet — the arc goes review → proposal"}

    user = (
        f"# Direction\n{d['statement']}\n\n"
        f"# Stakes\n{d.get('stakes') or '(not recorded)'}\n\n"
        f"# Your existing claim goals (fold these in — refine, don't contradict)\n{d.get('goals') or '(none)'}\n\n"
        f"# Your literature review (ground the questions in ITS gaps)\n{review['body_md'][:4000]}\n\n"
        f"# Lab capabilities & constraints (every hypothesis MUST be testable inside this)\n{LAB_CONSTRAINTS}\n\n"
        f"{_catalog_block()}"
        "# Task\nWrite the RESEARCH PROPOSAL by FILLING OUT the lab's template below.\n"
        "- `research_questions`: 1-4, from the review's gaps.\n"
        "- `hypotheses`: 2-6 falsifiable (each: hid H1.., statement, deciding metric, threshold decision "
        "rule, and a dataset_plan that — WHERE a listed REAL /data dataset fits — NAMES it (dataset + "
        "slice); synthesised/built-in only with a one-line justification when no real set fits). Prefer "
        "hypotheses the lab can settle on REAL data.\n"
        "- `method_plan`: the experiment series on THIS lab's hardware. `success_criteria`: what "
        "concludes the direction; what kills it.\n"
        "- `body_md`: the FULL proposal following the template — keep every `##` heading and the tables; "
        "replace each [bracketed] placeholder with real content; its Hypotheses section MUST match the "
        "structured `hypotheses` (same hid/metric/threshold). Use 'none identified' where empty.\n"
        "Output JSON for: title, research_questions, hypotheses, method_plan, success_criteria, body_md.\n\n"
        f"# TEMPLATE — fill this out\n{_PROPOSAL_TEMPLATE}"
    )
    try:
        proposal = await complete_validated(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            ResearchProposal,
            invocation_type="ariadne.propose",
            step_name="ariadne.propose",
            primary_model=ARIADNE_MODEL,
        )
    except ValidationError as e:
        log.warning("ariadne scholarship: proposal for #%s failed schema validation after retry: %s", claim_id, e)
        return {"claim_id": claim_id, "persisted": False, "reason": "schema_invalid"}

    # The document of record is the PI's template-filled body_md; the structured fields ride in meta
    # (the experiment designer consumes meta.hypotheses). Fall back to a derived body only if the
    # model somehow returned an empty body_md.
    body = proposal.body_md.strip() or (
        "## Research questions\n"
        + "\n".join(f"- {q}" for q in proposal.research_questions)
        + "\n\n## Hypotheses\n"
        + "\n".join(
            f"- **{h.hid}** {h.statement}\n  - metric: {h.metric} · decision: {h.threshold}"
            + (f"\n  - data: {h.dataset_plan}" if h.dataset_plan else "")
            for h in proposal.hypotheses
        )
        + f"\n\n## Method plan\n{proposal.method_plan}\n\n## Success criteria\n{proposal.success_criteria}\n"
    )
    meta = {
        "research_questions": proposal.research_questions,
        "hypotheses": [h.model_dump() for h in proposal.hypotheses],
        "success_criteria": proposal.success_criteria,
    }
    doc_id = await persist_document(state, claim_id, "proposal", proposal.title, body, meta, [])
    await ingest_to_library(state, claim_id, "proposal", proposal.title, body, doc_id)
    log.info(
        "ariadne scholarship: proposal for #%s persisted (doc %s, %d RQs, %d hypotheses)",
        claim_id,
        doc_id,
        len(proposal.research_questions),
        len(proposal.hypotheses),
    )
    return {"claim_id": claim_id, "document_id": doc_id, "kind": "proposal", "persisted": True}
