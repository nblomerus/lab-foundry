"""
Evaluation loop schemas.

Two stages:
  - EvidenceCrossCheck — per-finding, the structured report of "does each of
    this finding's claims actually trace to a quote or experiment in the
    evidence trail, and how substantive is the finding overall?"
  - BatchScores — re-uses the legacy AuditBatch shape so downstream code
    (state.update_finding_audit, slop circuit-breaker, dissent narrative)
    sees no schema change.

The cross-check is the genuine multi-step lift: today's single-call evaluation
has to share its context budget across N findings — quotes get truncated
to 240 chars per item, only 20 pages × 3 items shown. Splitting per-finding
lets each cross-check see the FULL evidence relevant to its finding, so
groundedness judgments aren't constrained by aggregate prompt size.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClaimCheck(BaseModel):
    """One specific claim inside a finding, traced back to the evidence."""

    claim: str = Field(..., description="A specific claim from the finding's summary, in your words.")
    quote: str | None = Field(
        None,
        description="Verbatim quote from the evidence trail that backs this claim, or null if no quote backs it.",
    )
    source_url: str | None = Field(None, description="URL of the page or experiment that backs this claim.")
    match: Literal["yes", "partial", "no"] = Field(
        ...,
        description="yes=quote/experiment directly supports the claim. "
        "partial=quote is related but the finding overreaches. "
        "no=no quote backs this claim (potentially fabricated).",
    )


class EvidenceCrossCheck(BaseModel):
    """One finding's cross-check report: claim-by-claim trace + overall verdict.

    The structured fields here are deliberately small — this stage costs N×
    model calls per task, so the output budget per call is tight. Final
    pass/slop bands are produced by the batch_score stage that aggregates
    these reports.
    """

    finding_id: int
    claims: list[ClaimCheck] = Field(
        default_factory=list,
        description="3-6 specific claims pulled from the finding, each checked against the evidence.",
    )
    substance: Literal["low", "medium", "high"] = Field(
        ...,
        description="low=generic/could-be-written-without-research. "
        "medium=somewhat specific but vague in places. "
        "high=concrete numbers / named entities / specific events.",
    )
    duplicate_of_finding_id: int | None = Field(
        None,
        description="If this finding restates an earlier finding F#, set its id. "
        "Duplicates downgrade to at-best unclear in the batch_score stage.",
    )
    notes: str = Field("", description="One sentence: what's the most load-bearing observation here?")


class CrossCheckBatch(BaseModel):
    """Returned when cross-checks are run for many findings at once (legacy
    behaviour for very small batches or for testing). Not used in the live
    loop — the loop fires one EvidenceCrossCheck per finding in parallel.
    """

    checks: list[EvidenceCrossCheck]


# ---- Final scoring (reuses the legacy AuditScore / AuditBatch shape) ------
# Same wire format as the existing handler so downstream consumers (the
# state client, the dissent narrative, the slop breaker) don't change.


class AuditScore(BaseModel):
    finding_id: int
    audit_score: float = Field(..., ge=0.0, le=1.0, description="0 = slop, 1 = high-quality research.")
    verdict: Literal["pass", "slop", "unclear"]
    reasoning: str = Field(..., description="1-2 sentences justifying the verdict.")


class AuditBatch(BaseModel):
    scores: list[AuditScore]
