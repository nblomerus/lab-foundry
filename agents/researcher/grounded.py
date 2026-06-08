"""
Library-grounded Researcher — research-era execution (Stage 3, shadow-first).

Ariadne (PI) frames directions; the Planner turns an approved direction into concrete tasks
(survey / analyze / compare …). This is the agent that EXECUTES one such task — but against the
lab's own CERTIFIED LIBRARY, never the open web (that's the point of Mimir + the context graph).

For one task it: (1) retrieves the relevant corpus evidence (hybrid retrieval), (2) CONVERSES with
Mimir for a multi-hop, graph-grounded synthesis (the lab's signature capability), then (3) judges
the one thing the PI cares about — does the evidence SUPPORT the direction's expectation, push
toward its KILL-condition, or is it INCONCLUSIVE? The output is a structured GroundedFinding whose
citations are real retrieved titles (anti-hallucination, like Ariadne's grounded_in).

`investigate_task` WRITES NOTHING (and emits no events unless `emit=True`) — it's the shadow read
path. The advisory/active persist path (finding row + confidence/last_evidence_at feedback) is a
separate, later seam; this module is the engine both will call.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from agents.ariadne.loop import ARIADNE_MODEL
from agents.llm import _chain_complete, _strip_fences
from agents.mimir.ask import answer_question, retrieve

log = logging.getLogger(__name__)

VERDICTS = ("supports", "contradicts", "inconclusive")
BLOCKERS = ("none", "thin_corpus", "needs_experiment")


class GroundedFinding(BaseModel):
    """The Researcher's verdict on a task, grounded in the certified corpus."""

    verdict: str = Field(..., description="supports | contradicts | inconclusive — re the direction's expectation")
    blocker: str = Field(
        "none",
        description="when inconclusive, WHY: 'thin_corpus' (the Library lacks the papers to judge) "
        "| 'needs_experiment' (the goal is empirical and needs a run no literature can settle) | 'none'",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="how load-bearing the corpus evidence is for this verdict")
    summary: str = Field(..., description="what the corpus actually shows for this task (2-4 sentences)")
    key_evidence: list[str] = Field(
        default_factory=list,
        description="EXACT paper titles from the evidence shown that carry the verdict — never invented",
    )
    kill_condition_check: str = Field(
        ..., description="does anything in the evidence trip the direction's kill-condition? state it plainly"
    )
    gaps: list[str] = Field(default_factory=list, description="what is still missing in the corpus to settle this")
    acquire_queries: list[str] = Field(
        default_factory=list,
        description="ONLY when blocker=thin_corpus: 2-4 specific topics/papers to acquire so the next pass can judge",
    )
    next_step: str = Field(..., description="the single most useful next task this finding implies")


_SYSTEM = """You are a Researcher in an autonomous AI research lab. You investigate the lab's own
CERTIFIED LIBRARY (a ~46k-paper corpus with hybrid retrieval + a concept graph) — NEVER the open
web — to test one of the PI's research directions. You are given a TASK, the direction's GOAL (its
expectation + kill-condition), the retrieved corpus EVIDENCE, and MIMIR'S multi-hop synthesis.
Produce a grounded FINDING. Judge honestly against the goal:
- supports      — the corpus evidence backs the direction's expectation.
- contradicts   — the evidence pushes toward the kill-condition (the bet looks wrong/done).
- inconclusive  — the corpus can't settle it yet (say so; do NOT manufacture a verdict).
When inconclusive, set `blocker` to say WHY so the lab can act:
- thin_corpus      — the Library simply lacks the papers to judge; fill `acquire_queries` with the
                     specific literature to fetch so the next pass can settle it.
- needs_experiment — the goal is EMPIRICAL (a number to hit, a run to do) that no amount of reading
                     can settle; it needs the experiments agent, not more retrieval.
- none             — genuinely ambiguous despite adequate evidence.
Cite ONLY paper titles that appear in the evidence — never invent one. Be skeptical: an honest
'inconclusive' with named gaps is worth more than a confident, ungrounded claim. Output ONLY JSON."""

_SCHEMA_HINT = """Output JSON with exactly these keys:
{
 "verdict": "supports|contradicts|inconclusive",
 "blocker": "none|thin_corpus|needs_experiment",
 "confidence": 0.0-1.0,
 "summary": str,
 "key_evidence": [exact paper titles from the EVIDENCE block],
 "kill_condition_check": str,
 "gaps": [str],
 "acquire_queries": [str],
 "next_step": str
}"""


_QUERY_SYSTEM = """You turn a research TASK into 2-4 SHORT corpus search queries — the topical
terms, methods, and datasets a literature search would actually use, NOT the instruction itself.
A long instruction ("synthesize 5 falsifiable hypotheses about novel GP variants") retrieves
noise; the topical terms ("Gaussian process kernel design", "sparse Gaussian process
approximation", "deep Gaussian processes") retrieve the right papers. Each query is 3-7 words,
concrete, no instruction verbs. Output ONLY JSON: {"queries": [str, ...]}."""


async def _search_queries(ctx: dict, *, model: str) -> list[str]:
    """Formulate topical corpus queries from the task instruction (a researcher's first move).
    Falls back to the direction/description if extraction fails — never returns empty."""
    user = (
        f"Task: {ctx['description']}\nDirection it serves: {ctx['direction'][:240]}\n\n"
        "Give the corpus search queries that would surface the evidence to test this."
    )
    try:
        content = await _chain_complete(
            [{"role": "system", "content": _QUERY_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.1,
            invocation_type="researcher.investigate",
            step_name="queries",
            primary_model=model,
        )
        data = json.loads(_strip_fences(content))
        qs = [q.strip() for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
        return qs[:4] or [ctx["direction"][:120]]
    except Exception as e:  # noqa: BLE001 — fall back to a topical-ish default
        log.warning("researcher: query extraction failed (%s) — falling back", e)
        return [ctx["direction"][:120] or ctx["description"][:120]]


async def _task_context(pool, task_id: int) -> dict | None:
    """The direction + goal a task serves — what the finding must be judged against."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.id, t.task_type, t.description, t.claim_id, c.statement AS direction "
            "FROM tasks t LEFT JOIN claims c ON c.id = t.claim_id WHERE t.id = $1",
            task_id,
        )
        if row is None:
            return None
        goal = (
            await conn.fetchrow(
                "SELECT expectation, kill_condition FROM claim_goals WHERE claim_id = $1 ORDER BY id LIMIT 1",
                row["claim_id"],
            )
            if row["claim_id"]
            else None
        )
    return {
        "task_id": row["id"],
        "task_type": row["task_type"],
        "description": row["description"],
        "claim_id": row["claim_id"],
        "direction": row["direction"] or "(no direction linked)",
        "expectation": (goal["expectation"] if goal else None) or "(no explicit expectation)",
        "kill_condition": (goal["kill_condition"] if goal else None) or "(no explicit kill-condition)",
    }


async def investigate_task(state, task_id: int, *, model: str = ARIADNE_MODEL, emit: bool = False):
    """Investigate one research task against the Library. Returns (context, refs, mimir, finding).
    WRITES NOTHING; emits the Mimir conversation only when `emit` (the live path). Returns None if
    the task can't be found."""
    ctx = await _task_context(state.pool, task_id)
    if ctx is None:
        return None

    # (1) formulate topical search queries (a researcher's first move) — retrieving on the raw
    # instruction returns noise; the topical terms surface the right papers.
    queries = await _search_queries(ctx, model=model)
    ctx["queries"] = queries

    # (2) corpus evidence — hybrid retrieval over each query, merged + deduped (no LLM). The
    # titles the finding may cite.
    seen: set[int] = set()
    refs = []
    for q in queries:
        for r in await retrieve(q, k=6):
            if r.document_id not in seen:
                seen.add(r.document_id)
                refs.append(r)
    refs = refs[:12]
    evidence = (
        "\n".join(f"[{r.trust_tier}] {(r.title or 'untitled')[:100]} — {r.snippet[:240]}" for r in refs)
        or "(no corpus evidence retrieved)"
    )

    # (3) Mimir's multi-hop synthesis — anchored on the TOPICAL terms (not the instruction) so its
    # own retrieval lands on-topic too.
    question = (
        f"Regarding {', '.join(queries)} — as it bears on the direction '{ctx['direction'][:160]}' "
        f"(expectation: {ctx['expectation'][:160]}): what does the corpus show, which methods/"
        "results are well-established versus thin, and is there evidence for or against?"
    )
    mimir = await answer_question(question, k=8, state=(state if emit else None), asker="researcher")

    # (3) judge the evidence against the goal → a grounded finding.
    user = (
        f"# Task ({ctx['task_type']})\n{ctx['description']}\n\n"
        f"# Direction it serves\n{ctx['direction']}\n\n"
        f"# Goal\nEXPECTATION (what confirms it): {ctx['expectation']}\n"
        f"KILL-CONDITION (what refutes it / when to stop): {ctx['kill_condition']}\n\n"
        f"# Retrieved corpus evidence (cite ONLY these titles)\n{evidence}\n\n"
        f"# Mimir's multi-hop synthesis\n{mimir.answer}\n"
        f"{('Gaps Mimir flags: ' + '; '.join(mimir.gaps)) if mimir.gaps else ''}\n\n"
        f"# Task\nJudge the evidence against the goal and produce the finding. {_SCHEMA_HINT}"
    )
    content = await _chain_complete(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        temperature=0.2,
        invocation_type="researcher.investigate",
        step_name="investigate",
        primary_model=model,
    )
    finding = GroundedFinding.model_validate_json(_strip_fences(content))
    return ctx, refs, mimir, finding


def grade_finding(finding: GroundedFinding, refs) -> dict:
    """Anti-hallucination check: how much of the cited key_evidence resolves to a REAL retrieved
    title, and is the verdict well-formed. The feedback seam should only move confidence when a
    finding is grounded (cited evidence exists) — same discipline as Ariadne's citation grading."""
    titles = {(r.title or "").strip().lower() for r in refs if r.title}
    cited = [t for t in finding.key_evidence if t and t.strip()]
    resolved = [t for t in cited if t.strip().lower() in titles]
    return {
        "verdict_valid": finding.verdict in VERDICTS,
        "n_cited": len(cited),
        "n_resolved": len(resolved),
        "grounded": (len(resolved) / len(cited)) if cited else 0.0,
        "unresolved": [t for t in cited if t.strip().lower() not in titles][:5],
    }
