"""
Grade Ariadne's shadow output against CHECKABLE predicates (readiness Stage 7).

The readiness plan's advisory-mode gate, made machine-checkable instead of subjective:
- schema_valid          — structurally complete (≥3 directions, each with a novelty
                          rationale + ≥1 claim_goal).
- claim_goals_wellformed — every claim_goal has a non-empty expectation + kill_condition.
- directions_grounded   — every direction cites ≥1 grounded_in work.
- citations_resolved    — each grounded_in title RESOLVES to a certified in-corpus
                          document (corpus_search title match). This is the anti-
                          hallucination gate: "no novelty citation that isn't real."

Only the "useful direction tree" gate stays subjective (≥2/3 named raters) — everything
here is computed. `passed` requires schema_valid + 100% well-formed goals + 100% grounded
directions + ≥80% citations resolved (fuzz allowance for title-match).
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from agents.ariadne.schemas import ASSESSMENTS, AriadneOutput, ReflectionOutput
from agents.ariadne.scoring import is_wellformed
from library.corpus.tools import corpus_search

_STOP = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "in",
    "on",
    "with",
    "to",
    "via",
    "using",
    "from",
    "as",
    "at",
    "is",
    "are",
    "by",
    "be",
}


def _toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 1}


async def _resolves(title: str) -> bool:
    """A grounded_in citation resolves if some certified corpus doc's title substantially
    covers it (containment ≥ 0.5). corpus_search only returns certified, queryable docs."""
    t = _toks(title)
    if len(t) < 2:
        return False
    try:
        chunks = await corpus_search(title, k=6)
    except Exception:  # noqa: BLE001
        return False
    for c in chunks:
        dt = _toks(c.title or "")
        if not dt:
            continue
        inter = len(t & dt)
        # Resolved if the citation covers a real doc title, OR a real doc title is
        # mostly contained in the citation (handles verbose "title — section" citations).
        if inter / len(t) >= 0.5 or inter / len(dt) >= 0.6:
            return True
    return False


class GradeReport(BaseModel):
    schema_valid: bool
    claim_goals_wellformed: float
    directions_grounded: float
    citations_resolved: float
    scores_wellformed: float
    n_citations: int
    unresolved: list[str]
    passed: bool


async def grade(out: AriadneOutput) -> GradeReport:
    dirs = out.directions
    schema_valid = len(dirs) >= 3 and all(
        d.novelty_rationale.strip() and d.stakes.strip() and d.claim_goals for d in dirs
    )

    goals = [g for d in dirs for g in d.claim_goals]
    cg_wf = (sum(1 for g in goals if g.expectation.strip() and g.kill_condition.strip()) / len(goals)) if goals else 0.0
    dir_grounded = (sum(1 for d in dirs if d.grounded_in) / len(dirs)) if dirs else 0.0
    scores_wf = (sum(1 for d in dirs if is_wellformed(d.scores)) / len(dirs)) if dirs else 0.0

    cites = [c for d in dirs for c in d.grounded_in]
    resolved, unresolved = 0, []
    for c in cites:
        if await _resolves(c):
            resolved += 1
        else:
            unresolved.append(c)
    cite_res = (resolved / len(cites)) if cites else 0.0

    passed = bool(schema_valid and cg_wf == 1.0 and dir_grounded == 1.0 and cite_res >= 0.8 and scores_wf == 1.0)
    return GradeReport(
        schema_valid=schema_valid,
        claim_goals_wellformed=cg_wf,
        directions_grounded=dir_grounded,
        citations_resolved=cite_res,
        scores_wellformed=scores_wf,
        n_citations=len(cites),
        unresolved=unresolved[:8],
        passed=passed,
    )


class ReflectionGrade(BaseModel):
    verdicts_valid: float  # fraction referencing a REAL standing id with a valid assessment
    n_verdicts: int
    n_lessons: int
    invalid_refs: list[int]
    passed: bool


def grade_reflection(out: ReflectionOutput, valid_ids: list[int]) -> ReflectionGrade:
    """Reflection is well-formed iff every verdict references a real standing direction id
    with a valid assessment (the anti-hallucination gate: she can't steer invented claims)."""
    vids = set(valid_ids)
    valid, invalid_refs = 0, []
    for v in out.verdicts:
        if v.claim_id in vids and v.assessment in ASSESSMENTS:
            valid += 1
        else:
            invalid_refs.append(v.claim_id)
    frac = (valid / len(out.verdicts)) if out.verdicts else 0.0
    passed = bool(out.verdicts and frac == 1.0 and out.reprioritized_focus.strip())
    return ReflectionGrade(
        verdicts_valid=frac,
        n_verdicts=len(out.verdicts),
        n_lessons=len(out.lessons),
        invalid_refs=invalid_refs[:8],
        passed=passed,
    )
