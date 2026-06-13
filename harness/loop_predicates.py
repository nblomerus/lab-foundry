"""Canonical research-loop predicates — the ONE source for "which directions are live,
actionable, gate-approved, held".

Before this module the active-status set was duplicated as a tuple
(``harness/dispatch.ACTIVE_CLAIM``) and as an inline SQL string
(``harness/ariadne_pace._ACTIVE``), and the adjudication/gate predicate fragments were
re-inlined across the pacemaker, the closure ladder, the planner, and the API. The loop's
stage was thus re-derived independently in several places that could drift apart. Importing
from here keeps every reader byte-identical.

Pure constants — no DB access, no project imports — so anything can import it without a cycle.
"""

from __future__ import annotations

# A direction's status is "active" (still in play, not terminal) when it is one of these.
# Terminal statuses (concluded, invalidated, merged) are excluded. This is the agenda/budget
# denominator the pacemaker, closure ladder, and gate all reason over.
ACTIVE_STATUSES: tuple[str, ...] = ("proposed", "tested", "weakly_supported", "replicated")

# Inline SQL list form for f-string interpolation: ``... AND c.status IN {ACTIVE_SQL}``.
# Built from ACTIVE_STATUSES so the two can never drift; byte-identical to the prior literals.
ACTIVE_SQL: str = "(" + ",".join(f"'{s}'" for s in ACTIVE_STATUSES) + ")"

# Re-derived predicate fragments over a ``claims c`` row (parameter-free; safe to interpolate).
# These mirror the adjudication/gate checks the pacemaker and engine guards use.
GATE_APPROVED = "dg.status = 'approved'"
HELD = "EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'hold')"
PASSED = "EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id AND da.verdict = 'pass')"
UNADJUDICATED = "NOT EXISTS (SELECT 1 FROM direction_adjudications da WHERE da.claim_id = c.id)"
