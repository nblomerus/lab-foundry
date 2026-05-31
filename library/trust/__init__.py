"""Trust tiers + the deterministic classify_trust gate (MIMIR_WARDEN_SCOPE.md §4).

The Library's input gate: Mimir calls classify_trust(meta) before ingesting a
source, and only the ambiguous web_reputable/web_unknown boundary ever reaches
an LLM. Keeping this deterministic is what lets Mimir own ingest + certification
without being a biased self-certifier.
"""

from library.trust.classify import classify_trust
from library.trust.schemas import TRUST_TIERS, DocMeta, TrustClassification

__all__ = ["classify_trust", "DocMeta", "TrustClassification", "TRUST_TIERS"]
