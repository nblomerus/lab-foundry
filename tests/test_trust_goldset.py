"""
Regression guard for the Mimir trust gate: runs the frozen gold set
(eval/mimir/cases.py) through library.trust.classify_trust.

classify_trust is PURE (no DB / network / clock), so this runs in the normal suite
with no live :5432 — unlike the agentlab MIMIR_SUITE which hits real APIs. The full
report (accuracy + safety metrics) is `python -m eval.mimir.evaluate`.
"""

from __future__ import annotations

import pytest

from eval.mimir.cases import CASES, TRUSTED_TIERS
from library.trust import DocMeta, classify_trust


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_trust_gold_case(case):
    tc = classify_trust(DocMeta(**case["meta"]))
    got = {"tier": tc.tier, "blocked": tc.blocked, "needs_llm": tc.needs_llm}
    assert got == case["expect"], f"{case['cat']}/{case['id']}: {case['why']}"


def test_no_spoof_reaches_trusted_tier():
    """Safety invariant: a lookalike / adversarial source must never land in a
    trusted tier (unless its own expectation is a legitimate trusted host)."""
    for case in CASES:
        if case["cat"] != "spoof" or case["expect"]["tier"] in TRUSTED_TIERS:
            continue
        tc = classify_trust(DocMeta(**case["meta"]))
        assert tc.tier not in TRUSTED_TIERS, f"spoof {case['id']} leaked to {tc.tier}"


def test_hard_gates_block_every_tier():
    """A retraction or a blocked license must quarantine regardless of tier."""
    for case in CASES:
        if case["cat"] not in {"retracted", "license_block"}:
            continue
        tc = classify_trust(DocMeta(**case["meta"]))
        assert tc.blocked and tc.tier == "quarantined", f"hard-gate {case['id']} admitted at {tc.tier}"
