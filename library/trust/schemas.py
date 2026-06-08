"""Inputs / outputs for the deterministic trust classifier (MIMIR_WARDEN_SCOPE §4)."""

from __future__ import annotations

from pydantic import BaseModel, Field

# The ordered trust ladder. The denormalized "current" verdict lives on
# documents.trust_tier; certifications holds the immutable history.
TRUST_TIERS = (
    "quarantined",
    "user_asserted",
    "web_unknown",
    "web_reputable",
    "official_repo",
    "preprint",
    "peer_reviewed",
)


class DocMeta(BaseModel):
    """Pre-resolved document signals fed to classify_trust.

    DOI/arXiv/GitHub probes are resolved on the ingest side (behind fetch
    timeouts + cache) and passed in here, so classify_trust stays pure, fast,
    and hang-free (MIMIR_WARDEN_SCOPE §4 review fix).
    """

    source_url: str | None = None
    doi: str | None = None
    doi_resolves: bool = False
    arxiv_id: str | None = None
    license: str | None = None
    github_has_release: bool | None = None
    github_days_since_push: int | None = None
    retracted: bool = False  # arXiv withdrawal / DOI retraction -> hard-gate BLOCK
    # A retraction check was APPLICABLE (arXiv id / DOI present) but could not be
    # completed (probe outage). Distinct from retracted=False (verified clean) so the
    # gate can fail CLOSED instead of admitting a possibly-retracted source as clean.
    retraction_unverified: bool = False


class TrustClassification(BaseModel):
    tier: str
    blocked: bool = False  # license hard-gate -> BLOCK regardless of tier
    needs_llm: bool = False  # the lone web_reputable/web_unknown tie-breaker
    signals: dict = Field(default_factory=dict)  # falsifiable signals -> certifications.signals
    reason: str = ""
