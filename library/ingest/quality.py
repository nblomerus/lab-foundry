"""
Deterministic content-QUALITY gate for the Library (MIMIR_WARDEN_SCOPE §3).

Distinct from the source-TRUST gate (`library.trust`): trust asks "do we trust
the origin?" (peer-reviewed > preprint > official repo > reputable web > …);
quality asks "is this real, substantial content worth storing at all?". Both
must pass. The quality gate is applied to EVERY source before a document is
created, so error pages, JS-wall remnants, login walls, dataset-viewer errors,
and thin stubs never enter the corpus — no matter how reputable the source.

Deterministic (no LLM) so it scales to the continuous intake pump. Relevance /
credibility judgements stay with the scouts (topic-scoped) and Mimir's trust
gate; this is the mechanical floor on substance.
"""

from __future__ import annotations

from dataclasses import dataclass

# A document must carry at least this much real extracted text. Tuned high
# enough that a thin repo pointer, a dataset stub, or a boilerplate page fails,
# while a paper abstract, a real README, or a substantive web article clears it.
MIN_QUALITY_CHARS = 500

# Substrings that mark an error / wall / non-content page. Matched
# case-insensitively against the HEAD of the text — if the body opens with one
# of these, it isn't content and we reject regardless of source trust.
_JUNK_MARKERS: tuple[str, ...] = (
    "dataset viewer is not available",
    "the dataset could not be loaded",
    "the full dataset viewer is not available",
    "unexpected error",
    "page not found",
    "404 not found",
    "this page could not be found",
    "enable javascript",
    "please enable js",
    "requires javascript",
    "access denied",
    "are you a robot",
    "just a moment",
    "please wait for verification",
    "checking your browser",
    "sign in to continue",
    "log in to continue",
    "you need to enable cookies",
    "this content is not available",
    "content isn't available",
    "too many requests",
    "rate limit exceeded",
)

_HEAD_CHARS = 600


@dataclass(frozen=True)
class QualityVerdict:
    ok: bool
    reason: str
    chars: int


def assess_quality(text: str | None, source_kind: str = "") -> QualityVerdict:
    """Judge whether `text` is substantial, real content worth ingesting.

    Rejects (ok=False) when the body is too thin or opens with a known error /
    wall / non-content marker. `source_kind` is accepted for future per-kind
    tuning but the floor is currently uniform — applied to all sources."""
    body = (text or "").strip()
    n = len(body)
    if n < MIN_QUALITY_CHARS:
        return QualityVerdict(False, f"too thin ({n} < {MIN_QUALITY_CHARS} chars)", n)
    head = body[:_HEAD_CHARS].lower()
    for marker in _JUNK_MARKERS:
        if marker in head:
            return QualityVerdict(False, f"non-content page (matched {marker!r})", n)
    return QualityVerdict(True, "ok", n)
