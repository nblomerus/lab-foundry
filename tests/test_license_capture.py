"""License capture — the SPDX normaliser and the trust gate it feeds. Pure: no
network, no DB. (The live GitHub probe is exercised by the first-light runner.)"""

from agents.mimir.handler import _spdx_or_none
from library.trust import DocMeta, classify_trust


def test_spdx_keeps_real_licenses():
    assert _spdx_or_none({"spdx_id": "MIT"}) == "MIT"
    assert _spdx_or_none({"spdx_id": "Apache-2.0"}) == "Apache-2.0"
    assert _spdx_or_none({"spdx_id": "GPL-3.0"}) == "GPL-3.0"


def test_spdx_drops_unknown_and_missing():
    # GitHub uses NOASSERTION when it can't detect a standard license; treating
    # that as a real license would over-quarantine, so it normalises to None.
    assert _spdx_or_none({"spdx_id": "NOASSERTION"}) is None
    assert _spdx_or_none({"spdx_id": "NONE"}) is None
    assert _spdx_or_none({"spdx_id": None}) is None
    assert _spdx_or_none({}) is None
    assert _spdx_or_none(None) is None


def test_captured_license_feeds_the_hard_gate():
    # A captured restrictive license forces a BLOCK regardless of tier.
    blocked = classify_trust(DocMeta(source_url="https://x.example/p", license="all-rights-reserved"))
    assert blocked.blocked is True
    assert blocked.tier == "quarantined"

    # A permissive captured license does NOT block (it rides the normal ladder).
    ok = classify_trust(DocMeta(source_url="https://github.com/o/r", license="MIT"))
    assert ok.blocked is False
    assert ok.signals.get("license") == "mit"
