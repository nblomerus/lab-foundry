"""
Mimir network-signal probe evaluation — the safety-critical layer the trust gate
*depends on but cannot see*. classify_trust is provably correct (eval/mimir/cases),
but it trusts the DocMeta that `agents.mimir.handler._resolve_signals` fills via live
probes: DOI-resolves (→ peer_reviewed), GitHub release/push (→ official_repo), and —
most importantly — arXiv-withdrawal + Crossref-retraction (→ the BLOCK hard-gate).
If a retraction probe wrongly returns False, a retracted source enters the trusted
Library even though the gate logic is perfect.

This is a LIVE eval (arXiv / Crossref / doi.org / GitHub), so:
  * each probe group has a CANARY — a case whose answer we are certain of; if the
    canary fails, the API is down/rate-limited and the group is SKIPPED (not FAILED),
    so an outage never masquerades as a correctness failure;
  * the arXiv-withdrawal ground truth is discovered LIVE (search for withdrawn
    papers, confirm the probe flags them) — no guessed ids.

    python -m eval.mimir.probe_eval

SAFETY NOTE (by design): the retraction/withdrawal probes FAIL OPEN — on an API
outage or a coverage gap they return False ("never block on a miss"). So a withdrawn
arXiv paper ingested while arXiv is unreachable, or a retracted paper Crossref does
not flag (e.g. the Wakefield MMR DOI), is admitted despite a correct gate. This eval
surfaces that; closing it (e.g. re-check on a schedule, or a second retraction source)
is a policy decision.
"""

from __future__ import annotations

import asyncio
import logging

from agents.mimir import handler as H
from library.ingest.fetcher import search_arxiv

log = logging.getLogger("eval.mimir.probes")

# Verified against the live APIs 2026-06-06 (see the discovery run in the session).
DOI_RESOLVE_CANARY = ("10.1038/nature14539", True)
DOI_RESOLVE_CASES = [
    ("10.1038/nature14539", True, "known journal DOI resolves"),
    ("10.9999/definitely-not-real-xyz", False, "fabricated DOI does not resolve (cannot mint peer_reviewed)"),
]
DOI_RETRACT_CANARY = ("10.1016/S0140-6736(20)31180-6", True)  # Surgisphere/Lancet — Crossref flags it
DOI_RETRACT_CASES = [
    ("10.1016/S0140-6736(20)31180-6", True, "Surgisphere Lancet — Crossref flags the retraction"),
    ("10.1038/nature14539", False, "clean review paper — not retracted"),
    (
        "10.1016/S0140-6736(97)11096-0",
        False,
        "Wakefield MMR — retracted in reality but NOT flagged by Crossref (KNOWN coverage gap → fail-open)",
    ),
]
GITHUB_CANARY_URL = "https://github.com/pytorch/pytorch"
GITHUB_CASES = [
    ("https://github.com/pytorch/pytorch", True, True, "active maintained repo → official_repo signals"),
    ("https://github.com/openai/gpt-2", False, False, "frozen repo, no release / stale push → web_unknown"),
]


class Report:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []  # (group, status, detail)

    def case(self, group, ok, detail, got=None, exp=None):
        status = "PASS" if ok else "FAIL"
        extra = "" if ok else f"  (got {got!r}, expected {exp!r})"
        self.results.append((group, status, detail + extra))

    def skip(self, group, detail):
        self.results.append((group, "SKIP", detail))

    def render(self) -> int:
        groups: dict[str, list[tuple[str, str]]] = {}
        for g, s, d in self.results:
            groups.setdefault(g, []).append((s, d))
        print("\n" + "=" * 78)
        print("MIMIR NETWORK-SIGNAL PROBE EVAL — _resolve_signals (live APIs)")
        print("=" * 78)
        fails = 0
        for g, items in groups.items():
            p = sum(1 for s, _ in items if s == "PASS")
            f = sum(1 for s, _ in items if s == "FAIL")
            sk = sum(1 for s, _ in items if s == "SKIP")
            fails += f
            print(f"  {g:<16} PASS={p} FAIL={f} SKIP={sk}")
            for s, d in items:
                if s != "PASS":
                    print(f"      [{s}] {d}")
        print("=" * 78)
        print(
            "  NOTE: probes are now TRI-STATE (retracted / clean / unknown). On an "
            "outage they\n        return UNKNOWN, and ingest_source FAILS CLOSED by default "
            "(MIMIR_RETRACTION_STRICT)\n        — it HOLDS the source instead of admitting it. "
            "Residual gap: a Crossref coverage\n        miss (e.g. Wakefield) returns CLEAN, not "
            "unknown — needs a 2nd retraction source.\n        FAILs are real bugs; SKIPs are outages."
        )
        print()
        return 1 if fails else 0


async def _doi_resolves_group(rep: Report):
    canary_doi, canary_exp = DOI_RESOLVE_CANARY
    if await H._doi_resolves(canary_doi) != canary_exp:
        rep.skip("doi_resolves", "canary failed — doi.org unreachable; group skipped")
        return
    for doi, exp, why in DOI_RESOLVE_CASES:
        got = await H._doi_resolves(doi)
        rep.case("doi_resolves", got == exp, why, got, exp)


async def _doi_retracted_group(rep: Report):
    canary_doi, canary_exp = DOI_RETRACT_CANARY
    if await H._doi_retracted(canary_doi) != canary_exp:
        rep.skip("doi_retracted", "canary failed — Crossref unreachable/changed; group skipped")
        return
    for doi, exp, why in DOI_RETRACT_CASES:
        got = await H._doi_retracted(doi)
        rep.case("doi_retracted", got == exp, why, got, exp)


async def _github_group(rep: Report):
    has_rel, _days, _lic = await H._github_repo_signals(GITHUB_CANARY_URL)
    if has_rel is not True:
        rep.skip("github", "canary failed — GitHub API unreachable/rate-limited; group skipped")
        return
    for url, exp_release, exp_active, why in GITHUB_CASES:
        has_release, days, _lic = await H._github_repo_signals(url)
        active = days is not None and days < 365  # GITHUB_ACTIVE_DAYS in classify.py
        ok = (bool(has_release) == exp_release) and (active == exp_active)
        rep.case(
            "github", ok, why, {"release": has_release, "active": active}, {"release": exp_release, "active": exp_active}
        )


async def _arxiv_withdrawn_group(rep: Report):
    """Discover withdrawn papers LIVE, then confirm the probe flags them (no guessed ids).
    Must phrase-search the ABSTRACT field — a free-text "withdrawn" query returns papers
    ABOUT withdrawal, not actually-withdrawn ones (whose abstract IS the notice)."""
    try:
        res = await search_arxiv('abs:"this paper has been withdrawn"', max_results=12)
    except Exception:  # noqa: BLE001
        res = []
    withdrawn_ids = [r.arxiv_id for r in res if r.arxiv_id and H._WITHDRAWN_RE.search(r.abstract or "")]
    if not withdrawn_ids:
        rep.skip(
            "arxiv_withdrawn",
            "arXiv unreachable or no withdrawn papers surfaced — cannot verify now "
            "(probe fails OPEN here: a real withdrawal would be missed during this outage)",
        )
        return
    for aid in withdrawn_ids[:3]:
        got = await H._arxiv_withdrawn(aid)
        rep.case("arxiv_withdrawn", got is True, f"live-discovered withdrawn paper {aid} flagged", got, True)
    # negative control — a famous non-withdrawn paper must NOT be flagged
    got = await H._arxiv_withdrawn("1706.03762")
    rep.case("arxiv_withdrawn", got is False, "non-withdrawn control (1706.03762) not flagged", got, False)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    rep = Report()
    # Each group is independent + outage-isolated; run sequentially to be gentle on the APIs.
    await _doi_resolves_group(rep)
    await _doi_retracted_group(rep)
    await _github_group(rep)
    await _arxiv_withdrawn_group(rep)
    raise SystemExit(rep.render())


if __name__ == "__main__":
    asyncio.run(main())
