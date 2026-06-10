"""Gap-filling unit tests for Mimir's acquire / handler / collectors — the branches the
DB-backed suites (test_acquire/test_pi_acquire/test_researcher_acquire/test_mimir_certify/
test_collectors) leave uncovered. Everything external is mocked: no Postgres, no Ollama, no
network. scout_arxiv / ingest_source / classify_trust / _loop_enabled / httpx are all patched.

Targets: agents.mimir.acquire (_resolve_candidates, dedupe walk, already_have, rate cap,
malformed, reject, _reply), agents.mimir.handler (handle_source_discovered, sweep, the
stage/classify/finalize/block + retraction tri-state gate, the probe helpers), and the
collectors sweep gaps.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.mimir.acquire as A
import agents.mimir.collectors as C
import agents.mimir.handler as H
from agents.mimir.acquire import AcquireRequest, handle_acquire_requested, request_acquire
from library.ingest.scouts import SourceDescriptor
from library.trust import DocMeta, TrustClassification
from tests._helpers import ScriptedPool, make_state

pytestmark = pytest.mark.asyncio

_WHY = "this source grounds the speculative-decoding claim we are testing right now in detail"


# ── small builders / fakes ─────────────────────────────────────────────────────
class _Disp:
    """The minimal dispatcher acquire/handler reads: .state and optional router/curator/session."""

    def __init__(self, state, *, router=None, curator=None, session=None):
        self.state = state
        self.router = router
        self.curator = curator
        self.session = session


def _desc(canonical_key="2401.00001", *, source_kind="arxiv", title="Paper", arxiv_id=None):
    return SourceDescriptor(
        kind="paper",
        source_kind=source_kind,
        canonical_key=canonical_key,
        url=f"https://arxiv.org/abs/{canonical_key}",
        arxiv_id=arxiv_id or canonical_key,
        title=title,
        why="topic",
    )


def _aconst(val):
    async def f(*_a, **_k):
        return val

    return f


def _enable_loop(monkeypatch, *, on=True):
    """Patch _loop_enabled on BOTH modules (acquire imports it from handler)."""
    monkeypatch.setattr(A, "_loop_enabled", lambda: on)
    monkeypatch.setattr(H, "_loop_enabled", lambda: on)


# ══════════════════════════════════════════════════════════════════════════════
# acquire._resolve_candidates — each identifier branch + the multi-page query walk
# ══════════════════════════════════════════════════════════════════════════════
async def test_request_acquire_allow_list_and_emit():
    state = make_state()
    await request_acquire(state, AcquireRequest(requester="researcher", why=_WHY, arxiv_id="2401.1"))
    state.emit_corpus_event.assert_awaited_once()
    assert state.emit_corpus_event.await_args.args[0] == "acquire.requested"


async def test_request_acquire_rejects_unknown_requester():
    with pytest.raises(ValueError, match="not allowed"):
        await request_acquire(make_state(), AcquireRequest(requester="intern", why=_WHY, arxiv_id="2401.1"))


async def test_req_target_id_is_stable_int():
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.1")
    a = A._req_target_id(req)
    assert isinstance(a, int) and a > 0
    assert a == A._req_target_id(req)  # deterministic


async def test_resolve_candidates_arxiv_id():
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.12345")
    cands = await A._resolve_candidates(req)
    assert len(cands) == 1
    d = cands[0]
    assert d.source_kind == "arxiv"
    assert d.canonical_key == "2401.12345"
    assert d.arxiv_id == "2401.12345"
    assert d.url == "https://arxiv.org/abs/2401.12345"
    assert d.why == _WHY


async def test_resolve_candidates_doi():
    req = AcquireRequest(requester="pi", why=_WHY, kind="paper", doi="10.1000/xyz")
    cands = await A._resolve_candidates(req)
    assert len(cands) == 1
    d = cands[0]
    assert d.source_kind == "doi"
    assert d.canonical_key == "10.1000/xyz"
    assert d.doi == "10.1000/xyz"
    assert d.url == "https://doi.org/10.1000/xyz"


async def test_resolve_candidates_url():
    req = AcquireRequest(requester="pi", why=_WHY, url="https://example.org/paper.pdf")
    cands = await A._resolve_candidates(req)
    assert len(cands) == 1
    d = cands[0]
    assert d.source_kind == "web"
    assert d.canonical_key == "https://example.org/paper.pdf"
    assert d.url == "https://example.org/paper.pdf"


async def test_resolve_candidates_none_when_empty():
    req = AcquireRequest(requester="pi", why=_WHY)  # no identifier, no query
    assert await A._resolve_candidates(req) == []


async def test_resolve_candidates_query_walks_multiple_pages(monkeypatch):
    monkeypatch.setenv("MIMIR_ACQUIRE_PAGES", "3")
    seen_starts: list[int] = []

    async def _scout(topics, per_topic=8, start=0, sort="submittedDate"):
        seen_starts.append(start)
        # page 0 and 1 return a hit each, page 2 returns empty → loop breaks early
        if start >= 2 * per_topic:
            return []
        return [_desc(canonical_key=f"hit-{start}")]

    monkeypatch.setattr(A, "scout_arxiv", _scout)
    req = AcquireRequest(requester="pi", why=_WHY, query="speculative decoding")
    cands = await A._resolve_candidates(req, n=8)
    assert [c.canonical_key for c in cands] == ["hit-0", "hit-8"]
    assert seen_starts == [0, 8, 16]  # walked until the empty page
    assert all(c.why == _WHY for c in cands)  # why overwritten onto each hit


async def test_resolve_candidates_query_breaks_on_first_empty(monkeypatch):
    monkeypatch.setenv("MIMIR_ACQUIRE_PAGES", "5")
    calls = {"n": 0}

    async def _scout(topics, per_topic=8, start=0, sort="submittedDate"):
        calls["n"] += 1
        return []  # first page already empty → break immediately

    monkeypatch.setattr(A, "scout_arxiv", _scout)
    req = AcquireRequest(requester="pi", why=_WHY, query="nothing here")
    assert await A._resolve_candidates(req) == []
    assert calls["n"] == 1


async def test_resolve_candidates_pages_floor_at_one(monkeypatch):
    monkeypatch.setenv("MIMIR_ACQUIRE_PAGES", "0")  # max(1, 0) → still one page
    calls = {"n": 0}

    async def _scout(topics, per_topic=8, start=0, sort="submittedDate"):
        calls["n"] += 1
        return [_desc(canonical_key="only")]

    monkeypatch.setattr(A, "scout_arxiv", _scout)
    req = AcquireRequest(requester="pi", why=_WHY, query="q")
    cands = await A._resolve_candidates(req)
    assert [c.canonical_key for c in cands] == ["only"]
    assert calls["n"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# acquire.handle_acquire_requested — gate / malformed / cap / dedupe / ingest dispatch
# ══════════════════════════════════════════════════════════════════════════════
async def test_handle_acquire_gated_off_returns_none(monkeypatch):
    _enable_loop(monkeypatch, on=False)
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    assert await handle_acquire_requested({"payload": req.model_dump()}, _Disp(make_state())) is None


async def test_handle_acquire_malformed_request_skipped(monkeypatch):
    _enable_loop(monkeypatch)
    # 'why' too short (min_length=30) → AcquireRequest construction raises → skipped.
    res = await handle_acquire_requested({"payload": {"requester": "pi", "why": "short"}}, _Disp(make_state()))
    assert res == {"skipped": True, "reason": "malformed request"}


async def test_handle_acquire_rate_limited(monkeypatch):
    _enable_loop(monkeypatch)
    monkeypatch.setenv("MIMIR_ACQUIRE_CAP_PER_AGENT", "2")
    state = make_state(count_acquires_today=3)  # 3 > cap 2
    req = AcquireRequest(requester="novelty", why=_WHY, arxiv_id="2401.00009")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rate_limited"
    assert "daily acquire cap (2)" in res["reason"]


async def test_handle_acquire_cap_env_malformed_falls_back_to_20(monkeypatch):
    _enable_loop(monkeypatch)
    monkeypatch.setenv("MIMIR_ACQUIRE_CAP_PER_AGENT", "not-an-int")
    state = make_state(count_acquires_today=21)  # 21 > default 20
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00009")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rate_limited"
    assert "(20)" in res["reason"]


async def test_handle_acquire_unresolvable_rejected(monkeypatch):
    _enable_loop(monkeypatch)
    state = make_state(count_acquires_today=0)
    req = AcquireRequest(requester="pi", why=_WHY)  # nothing to resolve
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rejected"
    assert "could not resolve" in res["reason"]


async def test_handle_acquire_already_have_walks_all_candidates(monkeypatch):
    _enable_loop(monkeypatch)
    cands = [_desc("a"), _desc("b"), _desc("c")]
    monkeypatch.setattr(A, "_resolve_candidates", _aconst(cands))
    state = make_state(count_acquires_today=0, document_exists=True)  # every candidate already present
    req = AcquireRequest(requester="pi", why=_WHY, query="q")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "already_have"
    assert "all 3 top matches" in res["reason"]


async def test_handle_acquire_dedupe_picks_first_new(monkeypatch):
    _enable_loop(monkeypatch)
    cands = [_desc("have"), _desc("fresh")]
    monkeypatch.setattr(A, "_resolve_candidates", _aconst(cands))
    state = make_state(count_acquires_today=0)
    # first candidate exists, second does not → ingest the second.
    state.document_exists = AsyncMock(side_effect=[True, False])
    ingest = AsyncMock(return_value={"decision": "approve", "tier": "peer_reviewed", "document_id": 77})
    monkeypatch.setattr(A, "ingest_source", ingest)
    req = AcquireRequest(requester="pi", why=_WHY, query="q")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "fulfilled"
    assert res["document_id"] == 77
    assert "ingested at peer_reviewed" in res["reason"]
    # the SECOND (fresh) descriptor was the one staged
    assert ingest.await_args.args[0]["canonical_key"] == "fresh"


async def test_handle_acquire_ingest_block_rejects(monkeypatch):
    _enable_loop(monkeypatch)
    monkeypatch.setattr(A, "_resolve_candidates", _aconst([_desc("x")]))
    state = make_state(count_acquires_today=0, document_exists=False)
    monkeypatch.setattr(
        A, "ingest_source", _aconst({"decision": "block", "reason": "restrictive license", "document_id": 5})
    )
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rejected"
    assert res["reason"] == "restrictive license"
    assert res["document_id"] == 5


async def test_handle_acquire_ingest_skip_rejected_with_reason(monkeypatch):
    _enable_loop(monkeypatch)
    monkeypatch.setattr(A, "_resolve_candidates", _aconst([_desc("x")]))
    state = make_state(count_acquires_today=0, document_exists=False)
    # neither approve nor block (stage skip / dedupe / failure) → rejected with str(reason)
    monkeypatch.setattr(A, "ingest_source", _aconst({"skipped": True, "reason": "deduped at stage"}))
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rejected"
    assert res["reason"] == "deduped at stage"


async def test_handle_acquire_ingest_skip_no_reason_stringifies_result(monkeypatch):
    _enable_loop(monkeypatch)
    monkeypatch.setattr(A, "_resolve_candidates", _aconst([_desc("x")]))
    state = make_state(count_acquires_today=0, document_exists=False)
    monkeypatch.setattr(A, "ingest_source", _aconst({"failed": True}))  # no 'reason' key
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    res = await handle_acquire_requested({"payload": req.model_dump()}, _Disp(state))
    assert res["status"] == "rejected"
    assert "failed" in res["reason"]  # str(result)


# ── acquire._reply — emits the right event + payload for each status ───────────
async def test_reply_fulfilled_emits_acquire_fulfilled():
    state = make_state()
    req = AcquireRequest(requester="researcher", why=_WHY, arxiv_id="2401.00001", claim_id=42)
    out = await A._reply(state, req, status="fulfilled", reason="ok", document_id=9)
    assert out == {"status": "fulfilled", "reason": "ok", "document_id": 9}
    state.emit_corpus_event.assert_awaited_once()
    args, kwargs = state.emit_corpus_event.await_args
    assert args[0] == "acquire.fulfilled"
    assert kwargs["target_id"] == 9  # document_id used as target when present
    assert kwargs["payload"]["claim_id"] == 42
    assert kwargs["payload"]["status"] == "fulfilled"


async def test_reply_already_have_is_fulfilled_event():
    state = make_state()
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    await A._reply(state, req, status="already_have", reason="have it")
    assert state.emit_corpus_event.await_args.args[0] == "acquire.fulfilled"


async def test_reply_rejected_emits_rejected_event_with_req_target():
    state = make_state()
    req = AcquireRequest(requester="pi", why=_WHY, arxiv_id="2401.00001")
    await A._reply(state, req, status="rejected", reason="nope")  # no document_id
    args, kwargs = state.emit_corpus_event.await_args
    assert args[0] == "acquire.rejected"
    assert kwargs["target_id"] == A._req_target_id(req)  # falls back to the request hash


# ══════════════════════════════════════════════════════════════════════════════
# handler.handle_source_discovered + handle_sweep_requested
# ══════════════════════════════════════════════════════════════════════════════
async def test_source_discovered_gated_off_returns_none(monkeypatch):
    _enable_loop(monkeypatch, on=False)
    out = await H.handle_source_discovered({"payload": {"source": {"x": 1}}}, _Disp(make_state()))
    assert out is None


async def test_source_discovered_no_source_skipped(monkeypatch):
    _enable_loop(monkeypatch)
    out = await H.handle_source_discovered({"id": 3, "payload": {}}, _Disp(make_state()))
    assert out == {"skipped": True, "reason": "no source in payload"}


async def test_source_discovered_dispatches_to_ingest(monkeypatch):
    _enable_loop(monkeypatch)
    ingest = AsyncMock(return_value={"document_id": 1, "decision": "approve", "tier": "peer_reviewed"})
    monkeypatch.setattr(H, "ingest_source", ingest)
    disp = _Disp(make_state(), router="R", curator="K", session="S")
    src = {"source_kind": "arxiv", "canonical_key": "2401.0"}
    out = await H.handle_source_discovered({"payload": {"source": src}}, disp)
    assert out["decision"] == "approve"
    # router/curator/session were threaded through
    _, kw = ingest.await_args
    assert (kw["router"], kw["curator"], kw["session"]) == ("R", "K", "S")


async def test_sweep_gated_off_returns_none(monkeypatch):
    _enable_loop(monkeypatch, on=False)
    assert await H.handle_sweep_requested({"payload": {"topics": ["a"]}}, _Disp(make_state())) is None


async def test_sweep_runs_discovery_with_payload_topics(monkeypatch):
    _enable_loop(monkeypatch)
    sweep = AsyncMock(return_value={"scanned": 5, "discovered": 2})
    monkeypatch.setattr(H, "run_discovery_sweep", sweep)
    state = make_state()
    out = await H.handle_sweep_requested({"payload": {"topics": ["t1", "t2"]}}, _Disp(state))
    assert out == {"scanned": 5, "discovered": 2}
    sweep.assert_awaited_once_with(["t1", "t2"], state, sort="submittedDate")


async def test_sweep_no_payload_topics_passes_none(monkeypatch):
    _enable_loop(monkeypatch)
    sweep = AsyncMock(return_value={"scanned": 0, "discovered": 0})
    monkeypatch.setattr(H, "run_discovery_sweep", sweep)
    state = make_state()
    await H.handle_sweep_requested({"payload": {}}, _Disp(state))
    sweep.assert_awaited_once_with(None, state, sort="submittedDate")


# ══════════════════════════════════════════════════════════════════════════════
# handler.ingest_source — stage / classify / approve / block / retraction gate
# ══════════════════════════════════════════════════════════════════════════════
def _patch_stage(monkeypatch, staged):
    monkeypatch.setattr(H, "stage_source", _aconst(staged))


def _patch_classify(monkeypatch, tc):
    monkeypatch.setattr(H, "classify_trust", lambda meta: tc)


def _clean_signals(monkeypatch):
    """_resolve_signals leaves meta untouched (no network)."""
    monkeypatch.setattr(H, "_resolve_signals", _aconst(None))


async def test_ingest_stage_failure_returns_failed(monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("fetch exploded")

    monkeypatch.setattr(H, "stage_source", _boom)
    out = await H.ingest_source({"source_kind": "arxiv"}, make_state())
    assert out["failed"] is True
    assert "fetch exploded" in out["reason"]


async def test_ingest_stage_skip_returns_staged(monkeypatch):
    # no document_id / not awaiting mimir → deduped, returned verbatim
    _patch_stage(monkeypatch, {"document_id": None, "reason": "deduped"})
    out = await H.ingest_source({"source_kind": "arxiv"}, make_state())
    assert out == {"document_id": None, "reason": "deduped"}


async def test_ingest_awaiting_not_mimir_returns_staged(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 9, "awaiting": "someoneelse"})
    out = await H.ingest_source({"source_kind": "arxiv"}, make_state())
    assert out == {"document_id": 9, "awaiting": "someoneelse"}


async def test_ingest_blocked_by_license_gate(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 1, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="quarantined", blocked=True, reason="GPL hard-gate"))
    state = make_state(get_document={"source_url": "https://x", "license": "GPL-3.0"})
    out = await H.ingest_source({"source_kind": "github"}, state)
    assert out["decision"] == "block"
    assert out["reason"] == "GPL hard-gate"
    state.set_document_trust.assert_awaited()  # quarantine write happened
    state.emit_corpus_event.assert_awaited()  # mimir.ingest_blocked


async def test_ingest_approve_at_deterministic_floor(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 2, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="peer_reviewed", reason="resolving DOI"))
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"chunks": 10, "status": "ingested"}))
    state = make_state(get_document={"source_url": "https://x", "doi": "10.1/x"})
    out = await H.ingest_source({"source_kind": "arxiv"}, state)
    assert out["decision"] == "approve"
    assert out["tier"] == "peer_reviewed"
    assert out["used_llm"] is False
    assert out["chunks"] == 10  # embed_and_finalize result merged in
    # certification persisted as approve
    _, kw = state.append_certification.await_args
    assert kw["decision"] == "approve"


async def test_ingest_sets_resolved_license_onto_doc(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 3, "awaiting": "mimir"})

    async def _resolve(meta):
        meta.license = "MIT"  # the GitHub probe found a license

    monkeypatch.setattr(H, "_resolve_signals", _resolve)
    _patch_classify(monkeypatch, TrustClassification(tier="official_repo", reason="active repo"))
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"status": "ingested"}))
    state = make_state(get_document={"source_url": "https://github.com/a/b"})
    await H.ingest_source({"source_kind": "github"}, state)
    state.set_document_license.assert_awaited_once_with(3, "MIT")


async def test_ingest_retraction_unverified_blocks_when_strict(monkeypatch):
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "on")
    _patch_stage(monkeypatch, {"document_id": 4, "awaiting": "mimir"})

    async def _resolve(meta):
        meta.retraction_unverified = True  # probe couldn't verify

    monkeypatch.setattr(H, "_resolve_signals", _resolve)
    _patch_classify(monkeypatch, TrustClassification(tier="preprint", reason="arXiv preprint"))
    state = make_state(get_document={"arxiv_id": "2405.0"})
    out = await H.ingest_source({"source_kind": "arxiv"}, state)
    assert out["decision"] == "block"
    assert "unverified" in out["reason"]
    _, kw = state.append_certification.await_args
    assert kw["signals"]["retraction_unverified"] is True


async def test_ingest_retraction_unverified_admits_when_lenient(monkeypatch):
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "off")
    _patch_stage(monkeypatch, {"document_id": 5, "awaiting": "mimir"})

    async def _resolve(meta):
        meta.retraction_unverified = True

    monkeypatch.setattr(H, "_resolve_signals", _resolve)
    _patch_classify(monkeypatch, TrustClassification(tier="preprint", reason="arXiv preprint"))
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"status": "ingested"}))
    state = make_state(get_document={"arxiv_id": "2405.0"})
    out = await H.ingest_source({"source_kind": "arxiv"}, state)
    assert out["decision"] == "approve"
    # admit-and-flag: the unverified flag is recorded on the approve certification
    _, kw = state.append_certification.await_args
    assert kw["signals"]["retraction_unverified"] is True


async def test_ingest_llm_tiebreaker_approve(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 6, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="web_unknown", needs_llm=True, reason="ambiguous blog"))
    verdict = H.MimirVerdict(decision="approve", tier="web_reputable", reasons="reputable industry source x" * 2)
    monkeypatch.setattr(H, "_certify_llm", _aconst(verdict))
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"status": "ingested"}))
    state = make_state(get_document={"source_url": "https://blog.example/p"})
    out = await H.ingest_source({"source_kind": "web"}, state, router="R", curator="K")
    assert out["decision"] == "approve"
    assert out["tier"] == "web_reputable"
    assert out["used_llm"] is True


async def test_ingest_llm_tiebreaker_block(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 7, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="web_unknown", needs_llm=True, reason="ambiguous"))
    verdict = H.MimirVerdict(decision="block", tier="web_unknown", reasons="content-farm spam, not credible x")
    monkeypatch.setattr(H, "_certify_llm", _aconst(verdict))
    state = make_state(get_document={"source_url": "https://spam.example/p"})
    out = await H.ingest_source({"source_kind": "web"}, state, router="R", curator="K")
    assert out["decision"] == "block"
    assert out["used_llm"] is True


async def test_ingest_needs_llm_but_no_router_uses_floor(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 8, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="web_unknown", needs_llm=True, reason="floor"))
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"status": "ingested"}))
    state = make_state(get_document={"source_url": "https://blog.example/p"})
    out = await H.ingest_source({"source_kind": "web"}, state)  # router/curator None
    assert out["decision"] == "approve"
    assert out["tier"] == "web_unknown"
    assert out["used_llm"] is False


async def test_ingest_llm_returns_none_falls_to_floor(monkeypatch):
    _patch_stage(monkeypatch, {"document_id": 9, "awaiting": "mimir"})
    _clean_signals(monkeypatch)
    _patch_classify(monkeypatch, TrustClassification(tier="web_unknown", needs_llm=True, reason="floor"))
    monkeypatch.setattr(H, "_certify_llm", _aconst(None))  # LLM failed → deterministic floor
    monkeypatch.setattr(H, "embed_and_finalize", _aconst({"status": "ingested"}))
    state = make_state(get_document={"source_url": "https://blog.example/p"})
    out = await H.ingest_source({"source_kind": "web"}, state, router="R", curator="K")
    assert out["decision"] == "approve"
    assert out["used_llm"] is False


# ══════════════════════════════════════════════════════════════════════════════
# handler probe helpers — httpx / search_arxiv mocks (the retraction tri-state)
# ══════════════════════════════════════════════════════════════════════════════
class _FakeResp:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json


class _FakeClient:
    """An async-context httpx client whose .head/.get return canned responses by callable."""

    def __init__(self, *, head=None, get=None):
        self._head = head
        self._get = get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def head(self, url, **_kw):
        return self._head(url) if callable(self._head) else self._head

    async def get(self, url, **_kw):
        return self._get(url, **_kw) if callable(self._get) else self._get


def _patch_httpx(monkeypatch, client):
    monkeypatch.setattr(H.httpx, "AsyncClient", lambda *a, **k: client)


# ── _doi_resolves ──────────────────────────────────────────────────────────────
async def test_doi_resolves_true(monkeypatch):
    _patch_httpx(monkeypatch, _FakeClient(head=_FakeResp(200)))
    assert await H._doi_resolves("10.1/x") is True


async def test_doi_resolves_false_on_404(monkeypatch):
    _patch_httpx(monkeypatch, _FakeClient(head=_FakeResp(404)))
    assert await H._doi_resolves("10.1/x") is False


async def test_doi_resolves_false_on_exception(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(H.httpx, "AsyncClient", _raise)
    assert await H._doi_resolves("10.1/x") is False


# ── _github_repo_signals ────────────────────────────────────────────────────────
async def test_github_signals_too_few_path_parts():
    assert await H._github_repo_signals("https://github.com/onlyowner") == (None, None, None)


async def test_github_signals_non_200(monkeypatch):
    _patch_httpx(monkeypatch, _FakeClient(get=lambda url, **k: _FakeResp(404)))
    assert await H._github_repo_signals("https://github.com/a/b") == (None, None, None)


async def test_github_signals_happy_with_release_and_license(monkeypatch):
    def _get(url, **_k):
        if url.endswith("/releases"):
            return _FakeResp(200, [{"tag": "v1"}])
        return _FakeResp(200, {"pushed_at": "2020-01-01T00:00:00Z", "license": {"spdx_id": "MIT"}})

    _patch_httpx(monkeypatch, _FakeClient(get=_get))
    has_release, days, spdx = await H._github_repo_signals("https://github.com/a/b")
    assert has_release is True
    assert isinstance(days, int) and days > 0
    assert spdx == "MIT"


async def test_github_signals_uses_token_header(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    captured = {}

    def _get(url, **_kw):
        if url.endswith("/releases"):
            return _FakeResp(200, [])
        return _FakeResp(200, {"pushed_at": None, "license": None})

    def _factory(*a, **k):
        captured["headers"] = k.get("headers", {})
        return _FakeClient(get=_get)

    monkeypatch.setattr(H.httpx, "AsyncClient", _factory)
    has_release, days, spdx = await H._github_repo_signals("https://github.com/a/b")
    assert captured["headers"].get("Authorization") == "Bearer secret"
    assert (has_release, days, spdx) == (False, None, None)  # no release, no push date, no license


async def test_github_signals_exception_returns_none(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(H.httpx, "AsyncClient", _raise)
    assert await H._github_repo_signals("https://github.com/a/b") == (None, None, None)


async def test_spdx_or_none_filters_noassertion():
    assert H._spdx_or_none({"spdx_id": "MIT"}) == "MIT"
    assert H._spdx_or_none({"spdx_id": "NOASSERTION"}) is None
    assert H._spdx_or_none({"spdx_id": "NONE"}) is None
    assert H._spdx_or_none(None) is None
    assert H._spdx_or_none({}) is None


# ── _arxiv_withdrawn (tri-state) ────────────────────────────────────────────────
async def test_arxiv_withdrawn_true(monkeypatch):
    res = SimpleNamespace(abstract="This paper has been withdrawn by the authors.")
    monkeypatch.setattr(H, "search_arxiv", _aconst([res]))
    assert await H._arxiv_withdrawn("2405.0") is True


async def test_arxiv_withdrawn_false_clean(monkeypatch):
    res = SimpleNamespace(abstract="A normal abstract about transformers.")
    monkeypatch.setattr(H, "search_arxiv", _aconst([res]))
    assert await H._arxiv_withdrawn("2405.0") is False


async def test_arxiv_withdrawn_none_unreachable(monkeypatch):
    monkeypatch.setattr(H, "search_arxiv", _aconst([]))  # always empty → retried then None
    monkeypatch.setattr(H.asyncio, "sleep", _aconst(None))  # don't actually wait 2s
    assert await H._arxiv_withdrawn("2405.0") is None


async def test_arxiv_withdrawn_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def _search(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # first attempt misses → backoff + retry
        return [SimpleNamespace(abstract="clean")]

    monkeypatch.setattr(H, "search_arxiv", _search)
    monkeypatch.setattr(H.asyncio, "sleep", _aconst(None))
    assert await H._arxiv_withdrawn("2405.0") is False
    assert calls["n"] == 2


async def test_arxiv_withdrawn_search_raises_then_none(monkeypatch):
    async def _search(*_a, **_k):
        raise RuntimeError("arxiv 503")

    monkeypatch.setattr(H, "search_arxiv", _search)
    monkeypatch.setattr(H.asyncio, "sleep", _aconst(None))
    assert await H._arxiv_withdrawn("2405.0") is None


# ── _doi_retracted (tri-state) ──────────────────────────────────────────────────
async def test_doi_retracted_true_via_update_to(monkeypatch):
    body = {"message": {"update-to": [{"type": "retraction"}]}}
    _patch_httpx(monkeypatch, _FakeClient(get=lambda url, **k: _FakeResp(200, body)))
    assert await H._doi_retracted("10.1/x") is True


async def test_doi_retracted_true_via_relation(monkeypatch):
    body = {"message": {"relation": {"is-retracted-by": [{"id": "10.1/y"}]}}}
    _patch_httpx(monkeypatch, _FakeClient(get=lambda url, **k: _FakeResp(200, body)))
    assert await H._doi_retracted("10.1/x") is True


async def test_doi_retracted_false_clean(monkeypatch):
    body = {"message": {"update-to": [], "relation": {}}}
    _patch_httpx(monkeypatch, _FakeClient(get=lambda url, **k: _FakeResp(200, body)))
    assert await H._doi_retracted("10.1/x") is False


async def test_doi_retracted_none_on_non_200(monkeypatch):
    _patch_httpx(monkeypatch, _FakeClient(get=lambda url, **k: _FakeResp(503)))
    assert await H._doi_retracted("10.1/x") is None


async def test_doi_retracted_none_on_exception(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("crossref down")

    monkeypatch.setattr(H.httpx, "AsyncClient", _raise)
    assert await H._doi_retracted("10.1/x") is None


# ── _resolve_signals — wiring of the probes onto DocMeta ─────────────────────────
async def test_resolve_signals_github_overwrites_license(monkeypatch):
    monkeypatch.setattr(H, "_github_repo_signals", _aconst((True, 10, "Apache-2.0")))
    meta = DocMeta(source_url="https://github.com/a/b", license="MIT")
    await H._resolve_signals(meta)
    assert meta.github_has_release is True
    assert meta.github_days_since_push == 10
    assert meta.license == "Apache-2.0"  # probe license wins over staged


async def test_resolve_signals_doi_resolves_set(monkeypatch):
    monkeypatch.setattr(H, "_doi_resolves", _aconst(True))
    monkeypatch.setattr(H, "_doi_retracted", _aconst(False))
    meta = DocMeta(doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.doi_resolves is True
    assert meta.retracted is False
    assert meta.retraction_unverified is False


async def test_resolve_signals_doi_retracted_blocks(monkeypatch):
    monkeypatch.setattr(H, "_doi_resolves", _aconst(True))
    monkeypatch.setattr(H, "_doi_retracted", _aconst(True))
    meta = DocMeta(doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.retracted is True


async def test_resolve_signals_arxiv_withdrawn_blocks(monkeypatch):
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(True))
    meta = DocMeta(arxiv_id="2405.0")
    await H._resolve_signals(meta)
    assert meta.retracted is True
    assert meta.retraction_unverified is False


async def test_resolve_signals_arxiv_withdrawn_skips_doi_check(monkeypatch):
    # arXiv withdrawal (True) sets retracted → the `not meta.retracted` doi guard short-circuits.
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(True))
    monkeypatch.setattr(H, "_doi_resolves", _aconst(True))
    called = {"doi_retracted": 0}

    async def _dr(_doi):
        called["doi_retracted"] += 1
        return False

    monkeypatch.setattr(H, "_doi_retracted", _dr)
    meta = DocMeta(arxiv_id="2405.0", doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.retracted is True
    assert called["doi_retracted"] == 0  # doi retraction check skipped once arxiv already blocked


async def test_resolve_signals_arxiv_unverified_flag(monkeypatch):
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(None))  # probe couldn't verify
    meta = DocMeta(arxiv_id="2405.0")
    await H._resolve_signals(meta)
    assert meta.retraction_unverified is True
    assert meta.retracted is False


async def test_resolve_signals_arxiv_clean_admits(monkeypatch):
    # w is False (fetched, no withdrawal notice) → neither retracted nor unverified.
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(False))
    meta = DocMeta(arxiv_id="2405.0")
    await H._resolve_signals(meta)
    assert meta.retracted is False
    assert meta.retraction_unverified is False


async def test_resolve_signals_doi_unverified_flag(monkeypatch):
    monkeypatch.setattr(H, "_doi_resolves", _aconst(False))
    monkeypatch.setattr(H, "_doi_retracted", _aconst(None))  # Crossref outage
    meta = DocMeta(doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.retraction_unverified is True


async def test_resolve_signals_github_no_license_keeps_staged(monkeypatch):
    monkeypatch.setattr(H, "_github_repo_signals", _aconst((False, None, None)))  # probe found no license
    meta = DocMeta(source_url="https://github.com/a/b", license="MIT")
    await H._resolve_signals(meta)
    assert meta.license == "MIT"  # staged license retained when probe returns None


async def test_resolve_signals_non_github_host_skips_probe(monkeypatch):
    called = {"n": 0}

    async def _gh(_url):
        called["n"] += 1
        return (None, None, None)

    monkeypatch.setattr(H, "_github_repo_signals", _gh)
    meta = DocMeta(source_url="https://example.org/x")
    await H._resolve_signals(meta)
    assert called["n"] == 0  # not a github host → never probed


async def test_doc_meta_extracts_fields():
    meta = H._doc_meta({"source_url": "https://x", "doi": "10.1/x", "arxiv_id": "2405.0", "license": "MIT"})
    assert meta.source_url == "https://x"
    assert meta.doi == "10.1/x"
    assert meta.arxiv_id == "2405.0"
    assert meta.license == "MIT"
    assert meta.doi_resolves is False


async def test_loop_enabled_env_gate(monkeypatch):
    monkeypatch.delenv("MIMIR_LOOP", raising=False)
    assert H._loop_enabled() is False
    monkeypatch.setenv("MIMIR_LOOP", "on")
    assert H._loop_enabled() is True
    monkeypatch.setenv("MIMIR_LOOP", "v1")
    assert H._loop_enabled() is True
    monkeypatch.setenv("MIMIR_LOOP", "nope")
    assert H._loop_enabled() is False


async def test_retraction_strict_env_gate(monkeypatch):
    monkeypatch.delenv("MIMIR_RETRACTION_STRICT", raising=False)
    assert H._retraction_strict() is True
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "off")
    assert H._retraction_strict() is False
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "on")
    assert H._retraction_strict() is True


async def test_certify_llm_returns_verdict_and_swallows_errors():
    class _Curator:
        async def build(self, invocation_type, context):
            return {"prompt": "p"}

    class _Router:
        async def invoke(self, *, prompt, output_schema_class, session=None, step_name=None):
            return H.MimirVerdict(decision="approve", tier="web_reputable", reasons="credible source here"), 1

    out = await H._certify_llm({"title": "T", "source_url": "https://x"}, _Curator(), _Router(), None)
    assert out is not None and out.decision == "approve"

    class _Boom:
        async def build(self, *a, **k):
            raise RuntimeError("model down")

    assert await H._certify_llm({"source_url": "https://x"}, _Boom(), None, None) is None


# ══════════════════════════════════════════════════════════════════════════════
# collectors — sweep gaps (scout failure, cursor failure, ledger failure, dedupe)
# ══════════════════════════════════════════════════════════════════════════════
def _sweep_state(**returns):
    """A state whose discovery_offset/document_exists/discovery_filter_new/emit are presettable."""
    st = make_state(pool=ScriptedPool())
    st.discovery_offset = AsyncMock(return_value=returns.get("offset", 0))
    st.document_exists = AsyncMock(return_value=returns.get("exists", False))
    fil = returns.get("filter")
    st.discovery_filter_new = (
        AsyncMock(side_effect=fil) if callable(fil) else AsyncMock(return_value=fil if fil is not None else set())
    )
    return st


async def test_sweep_explicit_topics_emits_new(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("a"), _desc("b")]))
    st = _sweep_state(filter=lambda sk, keys, **k: set(keys))
    res = await C.run_discovery_sweep(["topic"], st)
    assert res["scanned"] == 2
    assert res["discovered"] == 2
    # source.discovered emitted per new + the library.trends digest
    types = [c.args[0] for c in st.emit_corpus_event.await_args_list]
    assert types.count("source.discovered") == 2
    assert "library.trends" in types


async def test_sweep_skips_already_ingested(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("a"), _desc("b")]))
    st = _sweep_state(filter=lambda sk, keys, **k: set(keys))
    st.document_exists = AsyncMock(side_effect=[True, False])  # first already in corpus
    res = await C.run_discovery_sweep(["topic"], st)
    assert res["scanned"] == 2
    assert res["discovered"] == 1


async def test_sweep_no_new_emits_no_trends(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("a")]))
    st = _sweep_state(filter=set())  # ledger surfaces nothing
    res = await C.run_discovery_sweep(["topic"], st)
    assert res["discovered"] == 0
    types = [c.args[0] for c in st.emit_corpus_event.await_args_list]
    assert "library.trends" not in types


async def test_sweep_scout_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")

    async def _boom(*_a, **_k):
        raise RuntimeError("arxiv 500")

    monkeypatch.setitem(C._SCOUTS, "arxiv", _boom)
    st = _sweep_state()
    res = await C.run_discovery_sweep(["topic"], st)
    assert res == {"scanned": 0, "discovered": 0, "topics": ["topic"]}


async def test_sweep_cursor_failure_defaults_offset_zero(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    seen_start = {}

    async def _scout(topics, per_topic=5, start=0, sort="submittedDate"):
        seen_start["start"] = start
        return [_desc("a")]

    monkeypatch.setitem(C._SCOUTS, "arxiv", _scout)
    st = _sweep_state(filter=lambda sk, keys, **k: set(keys))
    st.discovery_offset = AsyncMock(side_effect=RuntimeError("cursor broke"))
    res = await C.run_discovery_sweep(["topic"], st)
    assert seen_start["start"] == 0  # fell back to offset 0
    assert res["discovered"] == 1


async def test_sweep_ledger_failure_surfaces_all(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("a"), _desc("b")]))
    st = _sweep_state()
    st.discovery_filter_new = AsyncMock(side_effect=RuntimeError("ledger broke"))
    res = await C.run_discovery_sweep(["topic"], st)
    # ledger failed → fall back to surfacing every fresh key
    assert res["discovered"] == 2


async def test_sweep_dedupes_duplicate_descriptors(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    # the same (source_kind, canonical_key) appears twice in the scout output
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("dup"), _desc("dup")]))
    st = _sweep_state(filter=lambda sk, keys, **k: set(keys))
    res = await C.run_discovery_sweep(["topic"], st)
    assert res["discovered"] == 1  # emitted once despite the duplicate


async def test_sweep_plans_when_topics_none(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    monkeypatch.setitem(C._SCOUTS, "arxiv", _aconst([_desc("a")]))
    monkeypatch.setattr(C, "plan_sweep", _aconst((["planned-topic"], 12)))
    st = _sweep_state(filter=lambda sk, keys, **k: set(keys))
    res = await C.run_discovery_sweep(None, st)
    assert res["topics"] == ["planned-topic"]
    assert res["discovered"] == 1


async def test_sweep_topics_none_with_explicit_per_topic(monkeypatch):
    # topics None → plan_sweep runs, but an explicit per_topic is honoured over the plan's.
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    captured = {}

    async def _scout(topics, per_topic=5, start=0, sort="submittedDate"):
        captured["per_topic"] = per_topic
        return []

    monkeypatch.setitem(C._SCOUTS, "arxiv", _scout)
    monkeypatch.setattr(C, "plan_sweep", _aconst((["planned"], 99)))
    st = _sweep_state()
    await C.run_discovery_sweep(None, st, per_topic=4)
    assert captured["per_topic"] == 4  # explicit per_topic wins over plan's 99


async def test_sweep_explicit_topics_default_per_topic(monkeypatch):
    # topics given but per_topic None and not planned → _AGENDA_PER_TOPIC default branch
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv")
    captured = {}

    async def _scout(topics, per_topic=5, start=0, sort="submittedDate"):
        captured["per_topic"] = per_topic
        return []

    monkeypatch.setitem(C._SCOUTS, "arxiv", _scout)
    st = _sweep_state()
    await C.run_discovery_sweep(["topic"], st)
    assert captured["per_topic"] == C._AGENDA_PER_TOPIC


async def test_enabled_scout_names_unknown_falls_back(monkeypatch):
    monkeypatch.setenv("LIBRARY_SCOUTS", "bogus,alsobogus")
    assert C._enabled_scout_names() == ["arxiv"]
    monkeypatch.setenv("LIBRARY_SCOUTS", "arxiv,web")
    assert C._enabled_scout_names() == ["arxiv", "web"]


async def test_active_claim_topics_failure_returns_empty():
    st = make_state()
    st.get_active_claims = AsyncMock(side_effect=RuntimeError("claims down"))
    assert await C._active_claim_topics(st) == []


async def test_active_claim_topics_strips_and_filters_blank():
    st = make_state()
    st.get_active_claims = AsyncMock(
        return_value=[SimpleNamespace(statement="  hot topic  "), SimpleNamespace(statement="  ")]
    )
    assert await C._active_claim_topics(st) == ["hot topic"]


# ── plan_sweep / rotation / ariadne_active ──────────────────────────────────────
async def test_rotate_window_wraps():
    seq = ["a", "b", "c", "d"]
    assert C._rotate(seq, 0, 2) == ["a", "b"]
    assert C._rotate(seq, 1, 2) == ["c", "d"]
    assert C._rotate(seq, 2, 2) == ["a", "b"]  # wraps
    assert C._rotate([], 0, 3) == []  # empty seq
    assert C._rotate(["x"], 0, 5) == ["x"]  # k clamped to len


async def test_rotation_index_is_monotone_int():
    assert isinstance(C._rotation_index(), int)


async def test_ariadne_active_gate(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)
    assert C.ariadne_active() is True
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "1")
    assert C.ariadne_active() is False
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "true")
    assert C.ariadne_active() is False


async def test_plan_sweep_aggressive_when_ariadne_dark(monkeypatch):
    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "1")
    topics, per_topic = await C.plan_sweep(make_state())
    assert len(topics) == C._AGGRESSIVE_TOPICS
    assert per_topic == C._AGGRESSIVE_PER_TOPIC
    assert len(set(topics)) == len(topics)  # rotating slice has no dupes


async def test_plan_sweep_agenda_merges_claims_and_frontier(monkeypatch):
    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    monkeypatch.delenv("KNOWLEDGE_CORE_ONLY", raising=False)  # Ariadne active
    st = make_state()
    dup = [SimpleNamespace(statement="speculative decoding"), SimpleNamespace(statement="speculative decoding")]
    st.get_active_claims = AsyncMock(return_value=dup)
    topics, per_topic = await C.plan_sweep(st)
    assert "speculative decoding" in topics
    assert topics.count("speculative decoding") == 1  # deduped (case-folded)
    assert per_topic == C._AGENDA_PER_TOPIC
    assert len(topics) <= C._MAX_AGENDA_TOPICS


async def test_default_sweep_topics_thin_wrapper(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CORE_ONLY", "1")
    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    topics = await C.default_sweep_topics(make_state())
    assert isinstance(topics, list) and topics


async def test_discovery_topics_env_override(monkeypatch):
    monkeypatch.setenv("LIBRARY_TOPICS", " a , b ,, c ")
    assert C.discovery_topics() == ["a", "b", "c"]
    monkeypatch.delenv("LIBRARY_TOPICS", raising=False)
    assert len(C.discovery_topics()) >= 1  # falls back to the frontier default


async def test_source_target_id_stable():
    a = C._source_target_id("2401.0")
    assert isinstance(a, int) and a > 0
    assert a == C._source_target_id("2401.0")
