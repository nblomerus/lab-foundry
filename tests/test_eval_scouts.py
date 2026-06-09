"""Pytest coverage for the scout (discovery-layer) evaluation harness.

Target: eval/scouts/evaluate.py (was 0%). All seams mocked — NO real arXiv/SearXNG/
GitHub/OpenML/HF network and no Postgres/Neo4j/LLM:

  * _contract_issues / _relevance  — pure functions, called directly with hand-built
                                     SourceDescriptor objects (satisfying + violating the spec).
  * _reachable                     — httpx.AsyncClient is monkeypatched on the module under
                                     test so the 2xx / non-2xx / exception paths are deterministic.
  * _eval_scout                    — the scout callable + _reachable are stubbed; covers clean
                                     PASS, contract-violation/dup FAIL, robustness FAIL, scout-raise
                                     FAIL, and the EMPTY (source up) / SKIP (source down) split.
  * _render                        — driven over a results list spanning every status branch
                                     (capsys), asserting the fail-count return code.
  * main                           — per-scout eval is patched to a fixed list and SystemExit is
                                     asserted for a clean run (0) and a failing-scout run (1).
"""

from __future__ import annotations

import httpx
import pytest

import eval.scouts.evaluate as ev
from library.ingest.scouts import SourceDescriptor


# ── builders / stubs ────────────────────────────────────────────────────────────
def _sd(*, kind="paper", source_kind="arxiv", canonical_key="k1", url="http://x", title="t"):
    return SourceDescriptor(
        kind=kind,
        source_kind=source_kind,
        canonical_key=canonical_key,
        url=url,
        title=title,
    )


_SPEC = {
    "source_kind": "arxiv",
    "kind": "paper",
}


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Stand-in for httpx.AsyncClient used as an async context manager. `get` either returns
    a response with the configured status_code or raises the configured exception."""

    instances: list[_FakeClient] = []

    def __init__(self, *, status_code=200, raise_on_get=None, **kw):
        self.kwargs = kw
        self._status_code = status_code
        self._raise_on_get = raise_on_get
        self.get_urls: list[str] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url):
        self.get_urls.append(url)
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return _FakeResp(self._status_code)


def _patch_client(monkeypatch, *, status_code=200, raise_on_get=None):
    _FakeClient.instances = []

    def _factory(**kw):
        return _FakeClient(status_code=status_code, raise_on_get=raise_on_get, **kw)

    monkeypatch.setattr(ev.httpx, "AsyncClient", _factory)


def _scout_spec(monkeypatch, *, fn, name="arxiv"):
    """A SCOUTS-style spec dict whose `fn` is the supplied stub scout."""
    return {
        "name": name,
        "fn": fn,
        "topic": "transformer",
        "source_kind": "arxiv",
        "kind": "paper",
        "terms": ["transformer"],
        "reach": "http://reach",
    }


# ── _contract_issues ──────────────────────────────────────────────────────────────
def test_contract_issues_clean():
    assert ev._contract_issues(_sd(), _SPEC) == []


def test_contract_issues_wrong_source_kind():
    issues = ev._contract_issues(_sd(source_kind="web"), _SPEC)
    assert issues == ["source_kind='web'!='arxiv'"]


def test_contract_issues_wrong_kind():
    issues = ev._contract_issues(_sd(kind="web"), _SPEC)
    assert issues == ["kind='web'!='paper'"]


def test_contract_issues_blank_canonical_key():
    # whitespace-only key is treated as empty
    issues = ev._contract_issues(_sd(canonical_key="   "), _SPEC)
    assert issues == ["empty canonical_key"]


def test_contract_issues_blank_url():
    issues = ev._contract_issues(_sd(url="   "), _SPEC)
    assert issues == ["empty url (ingest needs a fetch target)"]


def test_contract_issues_none_url():
    issues = ev._contract_issues(_sd(url=None), _SPEC)
    assert issues == ["empty url (ingest needs a fetch target)"]


def test_contract_issues_all_violations_accumulate():
    issues = ev._contract_issues(_sd(source_kind="web", kind="code", canonical_key="", url=""), _SPEC)
    assert len(issues) == 4


# ── _relevance ──────────────────────────────────────────────────────────────────
def test_relevance_empty_descriptors_is_zero():
    assert ev._relevance([], ["transformer"]) == 0.0


def test_relevance_all_match():
    desc = [_sd(title="a transformer paper"), _sd(title="TRANSFORMER again")]
    assert ev._relevance(desc, ["transformer"]) == 1.0


def test_relevance_partial_match():
    desc = [_sd(title="transformer"), _sd(title="unrelated"), _sd(title="model"), _sd(title="nope")]
    assert ev._relevance(desc, ["transformer", "model"]) == 0.5


def test_relevance_title_none_counts_as_miss():
    desc = [_sd(title=None), _sd(title="transformer")]
    assert ev._relevance(desc, ["transformer"]) == 0.5


def test_relevance_special_chars_in_terms_escaped():
    # a regex metachar in a term must be matched literally, not as a pattern
    desc = [_sd(title="c++ runtime")]
    assert ev._relevance(desc, ["c++"]) == 1.0


# ── _reachable ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reachable_2xx_true(monkeypatch):
    _patch_client(monkeypatch, status_code=200)
    assert await ev._reachable("http://export.arxiv.org/q") is True


@pytest.mark.asyncio
async def test_reachable_non_2xx_false(monkeypatch):
    _patch_client(monkeypatch, status_code=412)
    assert await ev._reachable("http://openml/q") is False


@pytest.mark.asyncio
async def test_reachable_exception_false(monkeypatch):
    _patch_client(monkeypatch, raise_on_get=httpx.ConnectError("down"))
    assert await ev._reachable("http://down") is False


@pytest.mark.asyncio
async def test_reachable_github_token_adds_auth_header(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    _patch_client(monkeypatch, status_code=200)
    ok = await ev._reachable("https://api.github.com/search/repositories?q=test")
    assert ok is True
    headers = _FakeClient.instances[-1].kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_reachable_github_without_token_no_auth(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _patch_client(monkeypatch, status_code=200)
    await ev._reachable("https://api.github.com/search/repositories?q=test")
    headers = _FakeClient.instances[-1].kwargs["headers"]
    assert "Authorization" not in headers
    assert headers["User-Agent"] == "labfoundry-scout-eval"


# ── _eval_scout ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_eval_scout_clean_pass(monkeypatch):
    async def fn(topics, per_topic=5):
        if topics == []:
            return []
        return [_sd(canonical_key="a", title="transformer one"), _sd(canonical_key="b", title="x")]

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "PASS"
    assert res["count"] == 2
    assert res["dups"] == 0
    assert res["contract_issues"] == []
    assert res["robust"] is True
    assert res["relevance"] == 0.5


@pytest.mark.asyncio
async def test_eval_scout_contract_violation_fails(monkeypatch):
    async def fn(topics, per_topic=5):
        if topics == []:
            return []
        return [_sd(canonical_key="a", source_kind="web")]  # wrong source_kind

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "FAIL"
    assert res["contract_issues"]  # at least one issue recorded, keyed by canonical_key
    assert res["contract_issues"][0].startswith("a:")


@pytest.mark.asyncio
async def test_eval_scout_duplicate_keys_fail(monkeypatch):
    async def fn(topics, per_topic=5):
        if topics == []:
            return []
        return [_sd(canonical_key="dup"), _sd(canonical_key="dup")]

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "FAIL"
    assert res["dups"] == 1


@pytest.mark.asyncio
async def test_eval_scout_not_robust_fails(monkeypatch):
    # empty topics returns a non-empty list → robust False → FAIL even with clean contract
    async def fn(topics, per_topic=5):
        if topics == []:
            return [_sd()]
        return [_sd(canonical_key="a")]

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["robust"] is False
    assert res["status"] == "FAIL"


@pytest.mark.asyncio
async def test_eval_scout_empty_topics_raises_is_not_robust(monkeypatch):
    # the scout raising on empty topics is caught → robust False, then the real call runs
    async def fn(topics, per_topic=5):
        if topics == []:
            raise RuntimeError("boom on empty")
        return [_sd(canonical_key="a")]

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["robust"] is False
    assert res["status"] == "FAIL"  # not robust


@pytest.mark.asyncio
async def test_eval_scout_scout_raises_on_real_call(monkeypatch):
    async def fn(topics, per_topic=5):
        if topics == []:
            return []
        raise RuntimeError("net down")

    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "FAIL"
    assert "raised: net down" in res["error"]
    assert res["robust"] is True


@pytest.mark.asyncio
async def test_eval_scout_empty_result_source_up_is_EMPTY(monkeypatch):
    async def fn(topics, per_topic=5):
        return []  # both empty-topics and real call return []

    monkeypatch.setattr(ev, "_reachable", _async_const(True))
    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "EMPTY"
    assert "source is UP" in res["note"]
    assert res["robust"] is True


@pytest.mark.asyncio
async def test_eval_scout_empty_result_source_down_is_SKIP(monkeypatch):
    async def fn(topics, per_topic=5):
        return []

    monkeypatch.setattr(ev, "_reachable", _async_const(False))
    res = await ev._eval_scout(_scout_spec(monkeypatch, fn=fn))
    assert res["status"] == "SKIP"
    assert "unreachable" in res["note"]


def _async_const(value):
    async def _f(*_a, **_k):
        return value

    return _f


# ── _render ──────────────────────────────────────────────────────────────────────
def test_render_all_clean_returns_zero(capsys):
    results = [
        {
            "name": "arxiv",
            "status": "PASS",
            "count": 3,
            "dups": 0,
            "contract_issues": [],
            "relevance": 0.66,
            "robust": True,
        },
        {"name": "web", "status": "SKIP", "note": "outage"},
    ]
    code = ev._render(results)
    assert code == 0
    out = capsys.readouterr().out
    assert "arxiv" in out and "PASS" in out
    assert "SKIP" in out and "outage" in out


def test_render_counts_every_failure_branch(capsys):
    results = [
        {"name": "openml", "status": "EMPTY", "note": "scout ineffective"},
        {"name": "github", "status": "FAIL", "error": "raised: boom"},
        {
            "name": "web",
            "status": "FAIL",
            "count": 2,
            "dups": 1,
            "contract_issues": ["a: bad kind", "a: empty url"],
            "relevance": 0.0,
            "robust": False,
        },
    ]
    code = ev._render(results)
    assert code == 1  # 3 fails → non-zero exit
    out = capsys.readouterr().out
    assert "EMPTY" in out
    assert "raised: boom" in out
    assert "DUPS=1" in out
    assert "EMPTY-TOPIC-NOT-[]" in out
    assert "CONTRACT×2" in out
    assert "- a: bad kind" in out  # per-issue lines printed


def test_render_pass_with_dups_flag_but_no_error(capsys):
    # a FAIL status carrying dups but no contract issues / robust True still increments fails
    results = [
        {
            "name": "ds",
            "status": "FAIL",
            "count": 4,
            "dups": 2,
            "contract_issues": [],
            "relevance": 1.0,
            "robust": True,
        }
    ]
    code = ev._render(results)
    assert code == 1
    out = capsys.readouterr().out
    assert "dedup=BAD" in out


# ── main ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_main_clean_exits_zero(monkeypatch):
    # every per-scout eval returns a SKIP (no fails) → _render returns 0 → SystemExit(0)
    monkeypatch.setattr(ev, "_eval_scout", _async_const({"name": "x", "status": "SKIP", "note": "n"}))
    with pytest.raises(SystemExit) as ei:
        await ev.main()
    assert ei.value.code == 0


@pytest.mark.asyncio
async def test_main_failing_scout_exits_one(monkeypatch):
    monkeypatch.setattr(
        ev,
        "_eval_scout",
        _async_const({"name": "x", "status": "FAIL", "error": "raised: boom"}),
    )
    with pytest.raises(SystemExit) as ei:
        await ev.main()
    assert ei.value.code == 1
