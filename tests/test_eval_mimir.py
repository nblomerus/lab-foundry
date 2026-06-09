"""Tests for the Mimir eval harnesses: eval/mimir/evaluate.py (trust-gate gold set)
and eval/mimir/probe_eval.py (live network-signal probes).

evaluate.py is offline+deterministic (real classify_trust over the frozen gold set),
so _evaluate runs for real. probe_eval.py talks to live APIs — every probe seam
(handler._doi_resolves/_doi_retracted/_github_repo_signals/_arxiv_withdrawn and
search_arxiv) is patched so the group/canary/skip logic is exercised with no network.
"""

from __future__ import annotations

import re
import sys

import pytest

import eval.mimir.evaluate as ev_eval
import eval.mimir.probe_eval as pe

# ════════════════════════════ evaluate.py ════════════════════════════════════


def test_evaluate_runs_real_goldset_safely():
    res = ev_eval._evaluate()
    rows = res["rows"]
    assert rows, "the frozen gold set must be non-empty"
    for r in rows:
        assert set(r) >= {"case", "got", "exp", "ok", "false_admit", "spoof_leak", "over_block"}
        assert isinstance(r["ok"], bool)
    # The safety guarantee the harness exists to defend: zero on the real gold set.
    assert not any(r["false_admit"] for r in rows)
    assert not any(r["spoof_leak"] for r in rows)


def _erow(ok=True, fa=False, sl=False, ob=False, cat="tier", cid="c1"):
    return {
        "case": {"cat": cat, "id": cid, "why": "because"},
        "got": {"tier": "B", "blocked": False, "needs_llm": False},
        "exp": {"tier": "B", "blocked": False, "needs_llm": False},
        "ok": ok,
        "false_admit": fa,
        "spoof_leak": sl,
        "over_block": ob,
    }


def test_report_all_correct_returns_zero(capsys):
    code = ev_eval._report({"rows": [_erow(), _erow(cid="c2")]})
    out = capsys.readouterr().out
    assert code == 0
    assert "All cases match" in out
    assert "accuracy : 2/2" in out


def test_report_mismatch_without_safety_failure(capsys):
    code = ev_eval._report({"rows": [_erow(), _erow(ok=False, ob=True, cid="c2")]})
    out = capsys.readouterr().out
    assert code == 0  # over-block alone is not fatal
    assert "MISMATCHES" in out
    assert "OVER-BLOCK" in out


def test_report_false_admit_is_fatal(capsys):
    code = ev_eval._report({"rows": [_erow(ok=False, fa=True)]})
    assert code == 1
    assert "FALSE-ADMIT" in capsys.readouterr().out


def test_report_spoof_leak_is_fatal(capsys):
    code = ev_eval._report({"rows": [_erow(ok=False, sl=True, cat="spoof")]})
    assert code == 1
    assert "SPOOF-LEAK" in capsys.readouterr().out


def test_main_clean_exits_zero(monkeypatch):
    monkeypatch.setattr(ev_eval, "_evaluate", lambda: {"rows": [_erow()]})
    monkeypatch.setattr(sys, "argv", ["prog"])
    with pytest.raises(SystemExit) as e:
        ev_eval.main()
    assert e.value.code == 0


def test_main_strict_makes_any_mismatch_fatal(monkeypatch):
    monkeypatch.setattr(ev_eval, "_evaluate", lambda: {"rows": [_erow(ok=False, ob=True)]})
    monkeypatch.setattr(sys, "argv", ["prog", "--strict"])
    with pytest.raises(SystemExit) as e:
        ev_eval.main()
    assert e.value.code == 1


# ════════════════════════════ probe_eval.py ══════════════════════════════════


class _ArxivRes:
    def __init__(self, arxiv_id, abstract):
        self.arxiv_id = arxiv_id
        self.abstract = abstract


def test_report_render_pass_zero(capsys):
    rep = pe.Report()
    rep.case("doi_resolves", True, "resolves")
    assert rep.render() == 0
    assert "PASS=1" in capsys.readouterr().out


def test_report_render_fail_one(capsys):
    rep = pe.Report()
    rep.case("github", False, "mismatch", got=1, exp=2)
    assert rep.render() == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "got 1" in out


def test_report_skip_is_not_failure(capsys):
    rep = pe.Report()
    rep.skip("arxiv_withdrawn", "outage")
    assert rep.render() == 0
    assert "SKIP" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_doi_resolves_group_runs_cases(monkeypatch):
    async def _resolves(doi):
        return doi == "10.1038/nature14539"

    monkeypatch.setattr(pe.H, "_doi_resolves", _resolves)
    rep = pe.Report()
    await pe._doi_resolves_group(rep)
    statuses = [s for _, s, _ in rep.results]
    assert statuses == ["PASS", "PASS"]


@pytest.mark.asyncio
async def test_doi_resolves_group_canary_fail_skips(monkeypatch):
    async def _resolves(doi):
        return False  # canary expects True -> skip

    monkeypatch.setattr(pe.H, "_doi_resolves", _resolves)
    rep = pe.Report()
    await pe._doi_resolves_group(rep)
    assert [s for _, s, _ in rep.results] == ["SKIP"]


@pytest.mark.asyncio
async def test_doi_retracted_group_runs_cases(monkeypatch):
    async def _retracted(doi):
        return doi == "10.1016/S0140-6736(20)31180-6"

    monkeypatch.setattr(pe.H, "_doi_retracted", _retracted)
    rep = pe.Report()
    await pe._doi_retracted_group(rep)
    assert [s for _, s, _ in rep.results] == ["PASS", "PASS", "PASS"]


@pytest.mark.asyncio
async def test_doi_retracted_group_canary_fail_skips(monkeypatch):
    async def _retracted(doi):
        return False

    monkeypatch.setattr(pe.H, "_doi_retracted", _retracted)
    rep = pe.Report()
    await pe._doi_retracted_group(rep)
    assert [s for _, s, _ in rep.results] == ["SKIP"]


@pytest.mark.asyncio
async def test_github_group_runs_cases(monkeypatch):
    async def _signals(url):
        if "pytorch" in url:
            return (True, 10, "MIT")
        return (False, 1000, None)

    monkeypatch.setattr(pe.H, "_github_repo_signals", _signals)
    rep = pe.Report()
    await pe._github_group(rep)
    assert [s for _, s, _ in rep.results] == ["PASS", "PASS"]


@pytest.mark.asyncio
async def test_github_group_canary_fail_skips(monkeypatch):
    async def _signals(url):
        return (False, None, None)  # canary has_rel not True -> skip

    monkeypatch.setattr(pe.H, "_github_repo_signals", _signals)
    rep = pe.Report()
    await pe._github_group(rep)
    assert [s for _, s, _ in rep.results] == ["SKIP"]


def _patch_arxiv(monkeypatch, results, withdrawn_fn):
    async def _search(query, max_results=12):
        return results

    monkeypatch.setattr(pe, "search_arxiv", _search)
    monkeypatch.setattr(pe.H, "_WITHDRAWN_RE", re.compile("withdrawn", re.I))
    monkeypatch.setattr(pe.H, "_arxiv_withdrawn", withdrawn_fn)


@pytest.mark.asyncio
async def test_arxiv_withdrawn_group_runs(monkeypatch):
    results = [
        _ArxivRes("2401.00001", "This paper has been withdrawn by the authors."),
        _ArxivRes("2401.00002", "An ordinary abstract about transformers."),
    ]

    async def _withdrawn(aid):
        return aid != "1706.03762"  # control id is the famous non-withdrawn paper

    _patch_arxiv(monkeypatch, results, _withdrawn)
    rep = pe.Report()
    await pe._arxiv_withdrawn_group(rep)
    # one live-discovered withdrawn paper (PASS) + the negative control (PASS)
    assert [s for _, s, _ in rep.results] == ["PASS", "PASS"]


@pytest.mark.asyncio
async def test_arxiv_withdrawn_group_skips_when_none_found(monkeypatch):
    async def _withdrawn(aid):
        return True

    _patch_arxiv(monkeypatch, [_ArxivRes("x", "nothing notable here")], _withdrawn)
    rep = pe.Report()
    await pe._arxiv_withdrawn_group(rep)
    assert [s for _, s, _ in rep.results] == ["SKIP"]


@pytest.mark.asyncio
async def test_arxiv_withdrawn_group_skips_on_search_error(monkeypatch):
    async def _boom(query, max_results=12):
        raise RuntimeError("arxiv down")

    monkeypatch.setattr(pe, "search_arxiv", _boom)
    rep = pe.Report()
    await pe._arxiv_withdrawn_group(rep)
    assert [s for _, s, _ in rep.results] == ["SKIP"]


@pytest.mark.asyncio
async def test_probe_main_all_groups_offline(monkeypatch):
    async def _resolves(doi):
        return doi == "10.1038/nature14539"

    async def _retracted(doi):
        return doi == "10.1016/S0140-6736(20)31180-6"

    async def _signals(url):
        return (True, 10, "MIT") if "pytorch" in url else (False, 1000, None)

    async def _withdrawn(aid):
        return aid != "1706.03762"

    monkeypatch.setattr(pe.H, "_doi_resolves", _resolves)
    monkeypatch.setattr(pe.H, "_doi_retracted", _retracted)
    monkeypatch.setattr(pe.H, "_github_repo_signals", _signals)
    _patch_arxiv(monkeypatch, [_ArxivRes("2401.1", "withdrawn by authors")], _withdrawn)

    with pytest.raises(SystemExit) as e:
        await pe.main()
    assert e.value.code == 0  # every group passed
