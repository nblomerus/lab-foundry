"""
Synthesis writes the ARTICLE — the final step of the traditional research arc.

Synthesis is already the lab's writer of record (experiments → paper-shaped finding);
this extends it one step: once a direction's question is settled (status `concluded`)
or its evidence is capped with a finding on file, `synthesis.article` composes the full
write-up from the dossier — Ariadne's literature review (related work) and proposal
(questions + hypotheses), the experiments actually run (real numbers + provenance), and
the finding(s). Decisive evidence reads as a paper; mixed evidence as an honest
research note. Mode-gated by the `synthesis` dial (this module's package).
"""

from __future__ import annotations

import json
import logging
import os

from pydantic import ValidationError

from agents.ariadne.scholarship import _grade_citations, _template, ingest_to_library, persist_document
from agents.llm import complete_validated
from agents.scholarship_schemas import Article
from harness.agent_modes import get_agent_mode

log = logging.getLogger(__name__)

SYNTH_MODEL = os.environ.get("SYNTHESIS_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))
CITATION_RESOLVE_MIN = 0.8
_ARTICLE_TEMPLATE = _template("article")  # the lab's house IMRaD template (agents/ariadne/templates/article.md)

_SYSTEM = (
    "You are the synthesis writer of an autonomous AI research lab, composing the lab's "
    "article from its own dossier. You are rigorous and honest: every number you report is "
    "one the lab actually measured (never invent or embellish results), every prior-work "
    "claim cites an EXACT corpus title from the review provided, and inconclusive evidence "
    "is written up as an honest research note, not inflated into a paper. Output ONLY JSON."
)


async def _dossier(state, claim_id: int) -> dict:
    review = await state.get_research_document(claim_id, "lit_review")
    proposal = await state.get_research_document(claim_id, "proposal")
    async with state.pool.acquire() as conn:
        direction = await conn.fetchrow(
            "SELECT id, statement, status FROM claims WHERE id = $1 AND claim_kind = 'direction'", claim_id
        )
        findings = await conn.fetch(
            "SELECT headline, claim_text, supported, confidence, key_numbers, limitations, n_experiments "
            "FROM research_findings WHERE direction_claim_id = $1 ORDER BY id",
            claim_id,
        )
        exps = await conn.fetch(
            "SELECT e.id, e.params->>'hypothesis' AS hypothesis, left(e.result::text, 400) AS result, "
            "left(e.interpretation, 300) AS interpretation, e.provenance "
            "FROM experiment_runs e JOIN tasks t ON t.id = e.task_id "
            "WHERE t.claim_id = $1 AND e.status = 'completed' ORDER BY e.id",
            claim_id,
        )
    findings_md = "\n".join(
        f"- [{f['supported']} · conf={float(f['confidence']):.2f} · n={f['n_experiments']}] {f['headline']}\n"
        f"  numbers: {f['key_numbers']}\n  limitations: {f['limitations']}"
        for f in findings
    )
    exps_md = "\n".join(
        f"- exp #{e['id']}: {e['hypothesis']}\n  result: {e['result']}\n  read: {e['interpretation']}\n"
        f"  provenance: {json.dumps(_obj(e['provenance']))[:220]}"
        for e in exps
    )
    return {
        "direction": dict(direction) if direction else None,
        "lit_review_md": review["body_md"] if review else None,
        "proposal_md": proposal["body_md"] if proposal else None,
        "findings_md": findings_md or None,
        "experiments_md": exps_md or None,
    }


def _obj(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


async def handle_synthesis_article(event: dict, dispatcher) -> dict | None:
    """`synthesis.article` → compose the article / research note from the direction's dossier."""
    state = dispatcher.state
    claim_id = (event.get("payload") or {}).get("claim_id")
    if claim_id is None or await get_agent_mode(state.pool, "synthesis") not in {"advisory", "active"}:
        return {"skipped": True, "reason": "no claim_id or synthesis paused"}
    dossier = await _dossier(state, claim_id)
    if dossier["direction"] is None:
        return {"skipped": True, "reason": f"direction {claim_id} not found"}
    if not dossier.get("findings_md"):
        return {"skipped": True, "reason": "no findings yet — the article waits for evidence", "claim_id": claim_id}

    d = dossier["direction"]
    user = (
        f"# Direction (the question this article answers) — status: {d['status']}\n{d['statement']}\n\n"
        f"# Research proposal (questions + hypotheses)\n{dossier.get('proposal_md') or '(no proposal recorded)'}\n\n"
        f"# Literature review (source for Related Work — keep its citations)\n"
        f"{(dossier.get('lit_review_md') or '(none)')[:4000]}\n\n"
        f"# The finding(s) the lab established\n{dossier['findings_md']}\n\n"
        f"# The experiments actually run (REAL numbers — report THESE)\n{dossier.get('experiments_md') or '(none)'}\n\n"
        "# Task\nWrite the ARTICLE by FILLING OUT the lab's template below. `body_md` MUST follow the "
        "template's section structure (keep every `##` heading); replace each [bracketed] placeholder "
        "with real content. Decisive evidence → a full paper; mixed/inconclusive → an honest research "
        "note that says exactly what was and wasn't settled. Report only REAL measured numbers (per "
        "hypothesis H1..); `citations` = the exact corpus titles used. Output JSON for: title, abstract, "
        "body_md (the filled template), citations.\n\n"
        f"# TEMPLATE — fill this out\n{_ARTICLE_TEMPLATE}"
    )
    try:
        article = await complete_validated(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            Article,
            invocation_type="synthesis.article",
            step_name="synthesis.article",
            primary_model=SYNTH_MODEL,
        )
    except ValidationError as e:
        log.warning("synthesis article: #%s failed schema validation after retry: %s", claim_id, e)
        return {"claim_id": claim_id, "persisted": False, "reason": "schema_invalid"}

    frac, unresolved = await _grade_citations(article.citations) if article.citations else (1.0, [])
    if article.citations and frac < CITATION_RESOLVE_MIN:
        log.warning(
            "synthesis article: #%s FAILED citation grading (%.0f%%, unresolved=%s) — not persisting",
            claim_id,
            frac * 100,
            unresolved[:4],
        )
        return {"claim_id": claim_id, "persisted": False, "reason": "citations_unresolved", "resolved": frac}

    body = f"## Abstract\n{article.abstract}\n\n{article.body_md}"
    meta = {"abstract": article.abstract, "citations_resolved": round(frac, 2), "direction_status": d["status"]}
    doc_id = await persist_document(state, claim_id, "article", article.title, body, meta, article.citations)
    await ingest_to_library(state, claim_id, "article", article.title, body, doc_id)
    log.info("synthesis: ARTICLE for direction #%s persisted (doc %s): %s", claim_id, doc_id, article.title[:80])
    return {"claim_id": claim_id, "document_id": doc_id, "kind": "article", "persisted": True}
