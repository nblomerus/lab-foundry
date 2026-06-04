"""Deterministic trust classifier — the ~95%-zero-token gate (MIMIR_WARDEN_SCOPE §4).

Pure function over a pre-resolved DocMeta: no network, no clock, no DB. The one
ambiguous boundary (web_reputable vs web_unknown) sets needs_llm=True so Mimir
may run a single tie-breaker; everything else is decided here for free.
"""

from __future__ import annotations

from urllib.parse import urlparse

from library.trust.schemas import DocMeta, TrustClassification

# Explicit restrictive licenses force a BLOCK regardless of tier. A missing/None
# license is NOT a block — papers and most web pages rarely declare one.
BLOCKED_LICENSES = frozenset({"none", "all-rights-reserved", "noindex"})

# Reputable reference hosts: a curated subset of fetcher._TTL_RULES. The social/
# news hosts there (reddit, HN, x) are caching rules, not trust signals, so they
# stay web_unknown. *.gov / *.edu suffixes are reputable by rule.
REPUTABLE_HOSTS = ("wikipedia.org", "docs.python.org", "developer.mozilla.org", "huggingface.co", "openml.org")
REPUTABLE_SUFFIXES = (".gov", ".edu")
GITHUB_ACTIVE_DAYS = 365


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _host_is(host: str, needle: str) -> bool:
    return host == needle or host.endswith("." + needle)


def _domain_reputable(host: str) -> bool:
    if any(_host_is(host, h) for h in REPUTABLE_HOSTS):
        return True
    return any(host.endswith(suffix) for suffix in REPUTABLE_SUFFIXES)


def classify_trust(meta: DocMeta) -> TrustClassification:
    host = _host(meta.source_url)
    lic = (meta.license or "").strip().lower()

    # Retraction/withdrawal hard-gate — a retracted source is never admitted,
    # regardless of where it sits on the tier ladder.
    if meta.retracted:
        return TrustClassification(
            tier="quarantined",
            blocked=True,
            signals={"host": host, "retracted": True},
            reason="source is retracted / withdrawn",
        )

    # License hard-gate next — independent of, and overriding, the tier ladder.
    if lic in BLOCKED_LICENSES:
        return TrustClassification(
            tier="quarantined",
            blocked=True,
            signals={"host": host, "license": lic},
            reason=f"license '{lic}' forbids retention",
        )

    sig = {"host": host, "license": lic or None}

    if meta.doi and meta.doi_resolves:
        return TrustClassification(tier="peer_reviewed", signals={**sig, "doi": meta.doi}, reason="resolving DOI")
    if meta.arxiv_id or _host_is(host, "arxiv.org"):
        return TrustClassification(tier="preprint", signals={**sig, "arxiv_id": meta.arxiv_id}, reason="arXiv preprint")
    if _host_is(host, "github.com"):
        active = bool(
            meta.github_has_release
            and meta.github_days_since_push is not None
            and meta.github_days_since_push < GITHUB_ACTIVE_DAYS
        )
        if active:
            return TrustClassification(
                tier="official_repo", signals={**sig, "github_active": True}, reason="active GitHub repo with releases"
            )
        return TrustClassification(
            tier="web_unknown", signals={**sig, "github_active": False}, reason="GitHub repo without recent release"
        )
    if _domain_reputable(host):
        return TrustClassification(tier="web_reputable", signals=sig, reason="reputable domain")

    # The lone ambiguous boundary: an LLM tie-breaker may run (and is capped at
    # web_reputable server-side — it can never mint a top-3 tier).
    return TrustClassification(tier="web_unknown", needs_llm=True, signals=sig, reason="unknown source — needs review")
