"""Unit tests for the deterministic trust classifier — pure, no DB / no network."""

from library.trust import DocMeta, classify_trust


def test_peer_reviewed_on_resolving_doi():
    tc = classify_trust(DocMeta(doi="10.1/x", doi_resolves=True, source_url="https://journal/x"))
    assert tc.tier == "peer_reviewed"
    assert not tc.needs_llm
    assert not tc.blocked


def test_doi_not_resolving_falls_through():
    tc = classify_trust(DocMeta(doi="10.1/x", doi_resolves=False, source_url="https://random.io/x"))
    assert tc.tier == "web_unknown"


def test_preprint_by_arxiv_id():
    assert classify_trust(DocMeta(arxiv_id="2401.00001")).tier == "preprint"


def test_preprint_by_arxiv_host():
    assert classify_trust(DocMeta(source_url="https://arxiv.org/abs/2401.1")).tier == "preprint"


def test_github_active_is_official_repo():
    tc = classify_trust(DocMeta(source_url="https://github.com/o/r", github_has_release=True, github_days_since_push=10))
    assert tc.tier == "official_repo"


def test_github_stale_or_no_release_is_web_unknown():
    no_release = DocMeta(source_url="https://github.com/o/r", github_has_release=False, github_days_since_push=10)
    stale = DocMeta(source_url="https://github.com/o/r", github_has_release=True, github_days_since_push=999)
    assert classify_trust(no_release).tier == "web_unknown"
    assert classify_trust(stale).tier == "web_unknown"


def test_reputable_domains():
    assert classify_trust(DocMeta(source_url="https://en.wikipedia.org/wiki/X")).tier == "web_reputable"
    assert classify_trust(DocMeta(source_url="https://nih.gov/p")).tier == "web_reputable"
    assert classify_trust(DocMeta(source_url="https://mit.edu/p")).tier == "web_reputable"


def test_unknown_source_needs_llm():
    tc = classify_trust(DocMeta(source_url="https://randomblog.io/p"))
    assert tc.tier == "web_unknown"
    assert tc.needs_llm is True


def test_social_hosts_are_web_unknown_not_reputable():
    # reddit / HN are TTL caching rules, NOT trust signals.
    assert classify_trust(DocMeta(source_url="https://reddit.com/r/x")).tier == "web_unknown"
    assert classify_trust(DocMeta(source_url="https://news.ycombinator.com/item?id=1")).tier == "web_unknown"


def test_license_hard_gate_blocks():
    tc = classify_trust(DocMeta(source_url="https://arxiv.org/abs/1", license="all-rights-reserved"))
    assert tc.blocked is True
    assert tc.tier == "quarantined"


def test_missing_license_does_not_block():
    tc = classify_trust(DocMeta(source_url="https://arxiv.org/abs/1", license=None))
    assert tc.blocked is False
    assert tc.tier == "preprint"


def test_license_gate_precedes_tier():
    # Even a resolving DOI is blocked by a forbidden license.
    tc = classify_trust(DocMeta(doi="10.1/x", doi_resolves=True, license="noindex"))
    assert tc.blocked is True
    assert tc.tier == "quarantined"
