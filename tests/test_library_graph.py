"""Tests for the library graph layer — concept extraction, field model, graph
read/write tools, and the claim sink. Everything external is mocked: Neo4j via
``tests._helpers.FakeNeoDriver`` (patched onto each module's ``_get_driver``),
Postgres via ``ScriptedPool``, and Ollama via a stubbed ``httpx.AsyncClient.post``.
No real Neo4j / Postgres / Ollama / network is touched.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from library.graph import extract as extract_mod
from library.graph import field_model as fm_mod
from library.graph import sink as sink_mod
from library.graph import tools as tools_mod
from tests._helpers import FakeNeoDriver, ScriptedPool

# ── extract.py: pure helpers ────────────────────────────────────────────────


def test_canon_key_alias_acronym():
    # Direct alias (spaced form) and plural acronym both canonicalize.
    assert extract_mod._canon_key("Large Language Models") == "llm"
    assert extract_mod._canon_key("LLMs") == "llm"
    assert extract_mod._canon_key("retrieval-augmented generation") == "rag"


def test_canon_key_mechanical_singular_and_hyphen():
    # No alias: lowercase, drop hyphens/spaces, conservative singular.
    assert extract_mod._canon_key("Fine-Tuning") == "finetuning"
    assert extract_mod._canon_key("Diffusion Models") == "diffusionmodel"
    # "ss" ending must NOT be singularized.
    assert extract_mod._canon_key("loss") == "loss"
    # short key (<=4) keeps trailing s.
    assert extract_mod._canon_key("bias") == "bias"


def test_canon_key_post_strip_alias_lookup():
    # "ConvNets" -> key "convnets" (len>4, ends s) -> singular "convnet" -> alias hit -> "cnn".
    assert extract_mod._canon_key("ConvNets") == "cnn"


def test_norm_valid_and_strip():
    c = extract_mod._norm("  Attention.  ")
    assert c == {"key": "attention", "name": "Attention"}


def test_norm_rejects_non_string():
    assert extract_mod._norm(123) is None
    assert extract_mod._norm(None) is None


def test_norm_rejects_too_short_long_and_no_alpha():
    assert extract_mod._norm("a") is None  # too short
    assert extract_mod._norm("x" * 61) is None  # too long
    assert extract_mod._norm("12345") is None  # no alpha
    assert extract_mod._norm("...") is None  # pure punctuation -> empty after strip


# ── extract.py: _parse ──────────────────────────────────────────────────────


def test_parse_clean_json_dedups_and_normalizes():
    raw = json.dumps(
        {
            "methods": ["LoRA", "lora", "attention"],  # LoRA/lora dedup
            "datasets": ["ImageNet"],
            "tasks": ["image classification"],
        }
    )
    out = extract_mod._parse(raw)
    assert [c["key"] for c in out["methods"]] == ["lora", "attention"]
    assert out["datasets"][0]["name"] == "ImageNet"
    assert out["tasks"][0]["key"] == "imageclassification"


def test_parse_strips_code_fences():
    raw = '```json\n{"methods": ["RAG"], "datasets": [], "tasks": []}\n```'
    out = extract_mod._parse(raw)
    assert out["methods"][0]["key"] == "rag"
    assert out["datasets"] == [] and out["tasks"] == []


def test_parse_embedded_object_recovery():
    # Not valid top-level JSON, but an object can be regex-extracted.
    raw = 'sure, here you go: {"methods": ["GAN"], "datasets": [], "tasks": []} done'
    out = extract_mod._parse(raw)
    assert out["methods"][0]["key"] == "gan"


def test_parse_no_object_returns_empty():
    out = extract_mod._parse("totally not json and no braces")
    assert out == {"methods": [], "datasets": [], "tasks": []}


def test_parse_malformed_braces_returns_empty():
    # Has braces but the inner content is not parseable JSON.
    out = extract_mod._parse("prefix {not: valid, json here} suffix")
    assert out == {"methods": [], "datasets": [], "tasks": []}


def test_parse_caps_at_eight_and_skips_junk():
    raw = json.dumps({"methods": ["m" + str(i) for i in range(12)] + ["!"], "datasets": None, "tasks": []})
    out = extract_mod._parse(raw)
    # Only first 8 of the input list are considered (the "!" at index 12 never reached).
    assert len(out["methods"]) == 8
    assert out["datasets"] == []  # None coerced to []


# ── extract.py: extract_paper_concepts (httpx mocked) ───────────────────────


def _ollama_post(payload_response: str, *, status: int = 200):
    """An AsyncMock for httpx.AsyncClient.post returning a canned Ollama reply."""

    async def _post(self, url, **kw):
        return httpx.Response(
            status,
            json={"response": payload_response},
            request=httpx.Request("POST", url),
        )

    return _post


@pytest.mark.asyncio
async def test_extract_paper_concepts_happy_path():
    body = json.dumps({"methods": ["attention"], "datasets": ["GLUE"], "tasks": []})
    with patch.object(httpx.AsyncClient, "post", new=_ollama_post(body)):
        out = await extract_mod.extract_paper_concepts("Some Title", "abstract text")
    assert out["methods"][0]["key"] == "attention"
    assert out["datasets"][0]["name"] == "GLUE"
    assert out["tasks"] == []


@pytest.mark.asyncio
async def test_extract_paper_concepts_truncates_and_defaults_title():
    captured = {}

    async def _post(self, url, **kw):
        captured["json"] = kw.get("json")
        return httpx.Response(200, json={"response": "{}"}, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "post", new=_post):
        await extract_mod.extract_paper_concepts("", None)
    # Empty title becomes "Untitled" in the prompt; empty body tolerated.
    assert "Untitled" in captured["json"]["prompt"]
    assert captured["json"]["format"] == "json"


@pytest.mark.asyncio
async def test_extract_paper_concepts_http_error_best_effort():
    # raise_for_status() on a 500 raises -> caught -> empty lists.
    with patch.object(httpx.AsyncClient, "post", new=_ollama_post("{}", status=500)):
        out = await extract_mod.extract_paper_concepts("T", "B")
    assert out == {"methods": [], "datasets": [], "tasks": []}


@pytest.mark.asyncio
async def test_extract_paper_concepts_network_exception_best_effort():
    async def _boom(self, url, **kw):
        raise httpx.ConnectError("no ollama")

    with patch.object(httpx.AsyncClient, "post", new=_boom):
        out = await extract_mod.extract_paper_concepts("T", "B")
    assert out == {"methods": [], "datasets": [], "tasks": []}


# ── extract.py: project + constraints + resume set (FakeNeoDriver) ──────────


@pytest.mark.asyncio
async def test_project_paper_concepts_writes_nodes_and_marker(monkeypatch):
    driver = FakeNeoDriver()
    monkeypatch.setattr(extract_mod, "_get_driver", AsyncMock(return_value=driver))
    concepts = {
        "methods": [{"key": "attention", "name": "Attention"}],
        "datasets": [{"key": "glue", "name": "GLUE"}],
        "tasks": [],  # empty kind is skipped
    }
    written = await extract_mod.project_paper_concepts(42, concepts)
    assert written == {"methods": 1, "datasets": 1, "tasks": 0}
    queries = [q for q, _ in driver.sessions[0].queries]
    # marker write + one MERGE per non-empty kind (methods, datasets) = 3 runs.
    assert any("concepts_extracted = true" in q for q in queries)
    assert any("METHOD" in q and "USES" in q for q in queries)
    assert any("DATASET" in q and "EVALUATED_ON" in q for q in queries)
    assert not any("TASK" in q for q in queries)  # tasks empty -> skipped


@pytest.mark.asyncio
async def test_project_paper_concepts_missing_kind_key(monkeypatch):
    # concepts dict missing a kind entirely -> treated as empty, no crash.
    driver = FakeNeoDriver()
    monkeypatch.setattr(extract_mod, "_get_driver", AsyncMock(return_value=driver))
    written = await extract_mod.project_paper_concepts(7, {})
    assert written == {"methods": 0, "datasets": 0, "tasks": 0}


@pytest.mark.asyncio
async def test_project_paper_concepts_best_effort_on_failure(monkeypatch):
    monkeypatch.setattr(extract_mod, "_get_driver", AsyncMock(side_effect=RuntimeError("graph down")))
    written = await extract_mod.project_paper_concepts(1, {"methods": [{"key": "a", "name": "A"}]})
    # Swallowed -> zero counts returned, no exception.
    assert written == {"methods": 0, "datasets": 0, "tasks": 0}


@pytest.mark.asyncio
async def test_ensure_concept_constraints(monkeypatch):
    driver = FakeNeoDriver()
    monkeypatch.setattr(extract_mod, "_get_driver", AsyncMock(return_value=driver))
    await extract_mod.ensure_concept_constraints()
    queries = [q for q, _ in driver.sessions[0].queries]
    assert any("method_key" in q for q in queries)
    assert any("dataset_key" in q for q in queries)
    assert any("task_key" in q for q in queries)
    assert any("paper_concepts_extracted" in q for q in queries)


@pytest.mark.asyncio
async def test_extracted_paper_ids(monkeypatch):
    def on_run(q, p):
        return [{"id": 1}, {"id": 2}, {"id": 2}]

    monkeypatch.setattr(extract_mod, "_get_driver", AsyncMock(return_value=FakeNeoDriver(on_run)))
    ids = await extract_mod.extracted_paper_ids()
    assert ids == {1, 2}


# ── field_model.py: _classify (pure) ────────────────────────────────────────


def test_classify_emerging():
    # tiny prior base, strong recent, below saturation threshold.
    state, vel = fm_mod._classify(total=6, recent_n=8, prior_n=1, n_recent=100, n_prior=100, sat_threshold=50)
    assert state == "emerging"
    assert vel == round(8 / 1 - 1.0, 3)


def test_classify_hot():
    # established + gaining share (velocity >= 0.25) but prior_n > 2 so not emerging.
    state, vel = fm_mod._classify(total=40, recent_n=10, prior_n=5, n_recent=100, n_prior=100, sat_threshold=50)
    assert state == "hot"
    assert vel > 0.25


def test_classify_declining():
    state, vel = fm_mod._classify(total=40, recent_n=2, prior_n=10, n_recent=100, n_prior=100, sat_threshold=50)
    assert state == "declining"
    assert vel <= -0.40


def test_classify_saturated():
    # total >= threshold, flat share (-0.25 < velocity < 0.25).
    state, vel = fm_mod._classify(total=60, recent_n=5, prior_n=5, n_recent=100, n_prior=100, sat_threshold=50)
    assert state == "saturated"
    assert vel == 0.0


def test_classify_stable_fallthrough():
    # not emerging/hot/declining/saturated -> stable.
    state, vel = fm_mod._classify(total=10, recent_n=1, prior_n=1, n_recent=100, n_prior=100, sat_threshold=50)
    assert state == "stable"


def test_classify_velocity_from_nothing():
    # prior share 0, recent share > 0 -> velocity pinned to 1.0.
    state, vel = fm_mod._classify(total=4, recent_n=3, prior_n=0, n_recent=100, n_prior=100, sat_threshold=50)
    assert vel == 1.0


def test_classify_velocity_zero_when_both_empty():
    state, vel = fm_mod._classify(total=4, recent_n=0, prior_n=0, n_recent=100, n_prior=100, sat_threshold=50)
    assert vel == 0.0
    assert state == "stable"


def test_classify_zero_cohort_sizes():
    # n_recent / n_prior == 0 -> shares default to 0.0, no ZeroDivision.
    state, vel = fm_mod._classify(total=4, recent_n=3, prior_n=2, n_recent=0, n_prior=0, sat_threshold=50)
    assert vel == 0.0


# ── field_model.py: build_field_model + read_field_brief ────────────────────


def _build_on_run(label_rows):
    """Build an on_run that returns cohort windows then per-label concept rows.

    `label_rows` maps a label substring (METHOD/TASK/DATASET) to a list of record
    dicts with keys key/name/total/recent_n/prior_n.
    """

    def on_run(q, p):
        if "ORDER BY n DESC LIMIT 6" in q:
            # two cohorts: recent 2506 (n=120), prior 2505 (n=100).
            return [{"ym": "2506", "n": 120}, {"ym": "2505", "n": 100}]
        for lbl, rows in label_rows.items():
            if f"(n:{lbl})" in q:
                return rows
        return []

    return on_run


@pytest.mark.asyncio
async def test_build_field_model_full(monkeypatch):
    label_rows = {
        "METHOD": [
            {"key": "attention", "name": "Attention", "total": 50, "recent_n": 30, "prior_n": 10},
            {"key": "hapax", "name": "Hapax", "total": 2, "recent_n": 1, "prior_n": 1},  # < MIN_TOTAL dropped
            {"key": "", "name": "", "total": 5, "recent_n": 2, "prior_n": 2},  # empty key/name dropped
        ],
        "TASK": [
            {"key": "qa", "name": "QA", "total": 10, "recent_n": 1, "prior_n": 8},  # declining
        ],
        "DATASET": [
            {"key": "glue", "name": "GLUE", "total": 8, "recent_n": 4, "prior_n": 4},  # flat
        ],
    }
    driver = FakeNeoDriver(_build_on_run(label_rows))
    monkeypatch.setattr(fm_mod, "_get_driver", AsyncMock(return_value=driver), raising=False)
    pool = ScriptedPool()
    summary = await fm_mod.build_field_model(driver, pool)

    assert summary["recent"] == "2506"
    assert summary["prior"] == "2505"
    assert summary["n_recent"] == 120 and summary["n_prior"] == 100
    # 3 concepts survive the MIN_TOTAL + non-empty filter (attention, qa, glue).
    assert summary["concepts"] == 3
    assert isinstance(summary["by_state"], dict)
    # DELETE + INSERT (executemany) hit the pool.
    sqls = [c[1] for c in pool.calls]
    assert any("DELETE FROM field_model" in s for s in sqls)
    assert any("INSERT INTO field_model" in s for s in sqls)


@pytest.mark.asyncio
async def test_build_field_model_raises_on_one_cohort(monkeypatch):
    def on_run(q, p):
        if "ORDER BY n DESC LIMIT 6" in q:
            return [{"ym": "2506", "n": 120}]  # only one cohort
        return []

    driver = FakeNeoDriver(on_run)
    pool = ScriptedPool()
    with pytest.raises(RuntimeError, match="< 2 dated paper cohorts"):
        await fm_mod.build_field_model(driver, pool)


@pytest.mark.asyncio
async def test_build_field_model_empty_concepts_uses_sentinel_threshold(monkeypatch):
    # Valid windows but no concept rows -> sat_threshold = 1<<30, no rows inserted.
    driver = FakeNeoDriver(_build_on_run({}))
    pool = ScriptedPool()
    summary = await fm_mod.build_field_model(driver, pool)
    assert summary["concepts"] == 0
    assert summary["sat_threshold"] == 1 << 30
    assert summary["by_state"] == {}


@pytest.mark.asyncio
async def test_windows_orders_recent_first():
    # The two top cohorts are re-sorted so the chronologically-later YM is "recent".
    def on_run(q, p):
        # prior listed first by popularity, but 2506 > 2505 so it must become recent.
        return [{"ym": "2505", "n": 200}, {"ym": "2506", "n": 150}, {"ym": "2504", "n": 50}]

    w = await fm_mod._windows(FakeNeoDriver(on_run))
    recent, prior, n_recent, n_prior = w
    assert recent == "2506" and prior == "2505"
    assert n_recent == 150 and n_prior == 200


@pytest.mark.asyncio
async def test_windows_none_when_too_few():
    w = await fm_mod._windows(FakeNeoDriver(lambda q, p: [{"ym": "2506", "n": 1}]))
    assert w is None


@pytest.mark.asyncio
async def test_read_field_brief_renders_buckets():
    rows = [
        {
            "concept_name": "RAG",
            "total_papers": 30,
            "velocity": 0.5,
            "trend_state": "hot",
            "recent_window": "2506",
            "prior_window": "2505",
        },
        {
            "concept_name": "Diffusion",
            "total_papers": 12,
            "velocity": 1.0,
            "trend_state": "emerging",
            "recent_window": "2506",
            "prior_window": "2505",
        },
        {
            "concept_name": "BERT-tuning",
            "total_papers": 80,
            "velocity": -0.6,
            "trend_state": "declining",
            "recent_window": "2506",
            "prior_window": "2505",
        },
    ]
    pool = ScriptedPool(rules=[("FROM field_model", rows)])
    brief = await fm_mod.read_field_brief(pool)
    assert "## Field model" in brief
    assert "2505→2506" in brief
    assert "RAG (30p, +50%)" in brief
    assert "Diffusion (12p, +100%)" in brief
    assert "BERT-tuning (80p, -60%)" in brief
    assert "HOT" in brief and "EMERGING" in brief and "DECLINING" in brief


@pytest.mark.asyncio
async def test_read_field_brief_respects_per_state_cap():
    rows = [
        {
            "concept_name": f"M{i}",
            "total_papers": 10 + i,
            "velocity": 0.3,
            "trend_state": "hot",
            "recent_window": "2506",
            "prior_window": "2505",
        }
        for i in range(5)
    ]
    pool = ScriptedPool(rules=[("FROM field_model", rows)])
    brief = await fm_mod.read_field_brief(pool, per_state=2)
    # only 2 of the 5 hot concepts rendered (M0, M1); the rest are capped out.
    assert "M0 (10p" in brief and "M1 (11p" in brief
    assert "M2 (" not in brief and "M3 (" not in brief and "M4 (" not in brief


@pytest.mark.asyncio
async def test_read_field_brief_empty_returns_blank():
    pool = ScriptedPool(rules=[("FROM field_model", [])])
    assert await fm_mod.read_field_brief(pool) == ""


# ── tools.py: _get_driver caching + read/write queries ──────────────────────


@pytest.mark.asyncio
async def test_get_driver_caches_singleton(monkeypatch):
    tools_mod._driver = None
    made = []

    def _factory(uri, auth=None):
        d = FakeNeoDriver()
        made.append(d)
        return d

    monkeypatch.setattr(tools_mod.AsyncGraphDatabase, "driver", staticmethod(_factory))
    try:
        d1 = await tools_mod._get_driver()
        d2 = await tools_mod._get_driver()
        assert d1 is d2
        assert len(made) == 1  # constructed once
    finally:
        tools_mod._driver = None


def _patch_tools_driver(monkeypatch, on_run=None):
    driver = FakeNeoDriver(on_run)
    monkeypatch.setattr(tools_mod, "_get_driver", AsyncMock(return_value=driver))
    return driver


@pytest.mark.asyncio
async def test_ensure_constraints(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.ensure_constraints()
    queries = [q for q, _ in driver.sessions[0].queries]
    assert any("claim_id" in q for q in queries)
    assert any("finding_claim_id" in q for q in queries)


@pytest.mark.asyncio
async def test_ensure_corpus_constraints(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.ensure_corpus_constraints()
    queries = [q for q, _ in driver.sessions[0].queries]
    assert any("paper_id" in q for q in queries)
    assert any("author_name" in q for q in queries)
    assert any("paper_doi" in q for q in queries)


@pytest.mark.asyncio
async def test_merge_claim(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_claim(1, "stmt", "open", 0.7)
    q, params = driver.sessions[0].queries[0]
    assert "MERGE (c:Claim" in q
    assert params["id"] == 1 and params["confidence"] == 0.7


@pytest.mark.asyncio
async def test_merge_finding_grounds_claim(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_finding_grounds_claim(
        finding_id=5,
        claim_id=2,
        source="web",
        url="http://x",
        title="T",
        summary="S",
        relevance_score=0.9,
        supports_claim=True,
        audit_verdict="pass",
        created_at="2026-01-01",
    )
    q, params = driver.sessions[0].queries[0]
    assert "MERGE (f:Finding" in q and "GROUNDS" in q
    assert params["finding_id"] == 5 and params["claim_id"] == 2


@pytest.mark.asyncio
async def test_merge_critic_verdict_with_citations(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_critic_verdict_challenged_claim(
        verdict_id=9,
        claim_id=2,
        verdict="weak",
        confidence=0.4,
        reasoning="r",
        action="flag",
        cited_finding_ids=[1, 2],
        created_at="2026-01-01",
    )
    queries = [q for q, _ in driver.sessions[0].queries]
    # verdict MERGE + a second CITED_BY UNWIND query.
    assert any("CHALLENGED" in q for q in queries)
    assert any("CITED_BY" in q for q in queries)


@pytest.mark.asyncio
async def test_merge_critic_verdict_without_citations(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_critic_verdict_challenged_claim(
        verdict_id=9,
        claim_id=2,
        verdict="weak",
        confidence=0.4,
        reasoning="r",
        action="flag",
        cited_finding_ids=[],
        created_at="2026-01-01",
    )
    queries = [q for q, _ in driver.sessions[0].queries]
    # no CITED_BY query when there are no cited findings.
    assert not any("CITED_BY" in q for q in queries)


@pytest.mark.asyncio
async def test_merge_paper_full(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_paper(
        3,
        doi="10.x",
        arxiv_id="2506.001",
        title="T",
        year=2026,
        trust_tier="A",
        source_url="http://s",
        authors=["Ada", "Linus"],
    )
    queries = [q for q, _ in driver.sessions[0].queries]
    assert any("MERGE (p:Paper" in q for q in queries)
    assert any("FROM" in q for q in queries)  # source edge
    assert any("BY" in q and "Author" in q for q in queries)  # author edge


@pytest.mark.asyncio
async def test_merge_paper_minimal_no_optional_edges(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_paper(3)
    queries = [q for q, _ in driver.sessions[0].queries]
    # only the node MERGE; no Source / Author runs.
    assert len(queries) == 1
    assert not any("Source" in q for q in queries)


@pytest.mark.asyncio
async def test_merge_paper_best_effort_on_failure(monkeypatch):
    monkeypatch.setattr(tools_mod, "_get_driver", AsyncMock(side_effect=RuntimeError("down")))
    # Must not raise.
    await tools_mod.merge_paper(3, title="T")


@pytest.mark.asyncio
async def test_merge_dataset(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.merge_dataset(4, name="GLUE", modality="text", task="nli")
    q, params = driver.sessions[0].queries[0]
    assert "MERGE (d:Dataset" in q and params["name"] == "GLUE"


@pytest.mark.asyncio
async def test_merge_dataset_best_effort_on_failure(monkeypatch):
    monkeypatch.setattr(tools_mod, "_get_driver", AsyncMock(side_effect=RuntimeError("down")))
    await tools_mod.merge_dataset(4, name="GLUE")  # swallowed


@pytest.mark.asyncio
async def test_link_finding_cites_paper(monkeypatch):
    driver = _patch_tools_driver(monkeypatch)
    await tools_mod.link_finding_cites_paper(5, 6, created_at="2026-01-01")
    q, params = driver.sessions[0].queries[0]
    assert "CITES" in q and params["finding_id"] == 5 and params["paper_id"] == 6


@pytest.mark.asyncio
async def test_link_finding_cites_paper_best_effort(monkeypatch):
    monkeypatch.setattr(tools_mod, "_get_driver", AsyncMock(side_effect=RuntimeError("down")))
    await tools_mod.link_finding_cites_paper(5, 6)  # swallowed


@pytest.mark.asyncio
async def test_get_claim_evidence_chain(monkeypatch):
    rows = [{"finding_id": 1, "source": "web", "relevance_score": 0.9}]
    _patch_tools_driver(monkeypatch, lambda q, p: rows)
    out = await tools_mod.get_claim_evidence_chain(2, limit=5)
    assert out == rows


@pytest.mark.asyncio
async def test_get_claim_critics(monkeypatch):
    rows = [{"verdict_id": 1, "verdict": "weak", "cited_finding_ids": [3]}]
    _patch_tools_driver(monkeypatch, lambda q, p: rows)
    out = await tools_mod.get_claim_critics(2)
    assert out == rows


@pytest.mark.asyncio
async def test_get_finding_influence_found(monkeypatch):
    rec = {"finding_id": 7, "claim_id": 2, "claim_statement": "x"}
    _patch_tools_driver(monkeypatch, lambda q, p: [rec])
    out = await tools_mod.get_finding_influence(7)
    assert out == rec


@pytest.mark.asyncio
async def test_get_finding_influence_not_found(monkeypatch):
    _patch_tools_driver(monkeypatch, lambda q, p: [])
    out = await tools_mod.get_finding_influence(7)
    assert out == {"finding_id": 7, "not_found": True}


# ── sink.py: claim.created projection ────────────────────────────────────────


class _Claim:
    def __init__(self, id, statement, status, confidence):
        self.id = id
        self.statement = statement
        self.status = status
        self.confidence = confidence


@pytest.mark.asyncio
async def test_sink_claim_created_writes(monkeypatch):
    merged = {}

    async def _merge_claim(cid, stmt, status, conf):
        merged.update(id=cid, statement=stmt, status=status, confidence=conf)

    monkeypatch.setattr(tools_mod, "merge_claim", _merge_claim)

    dispatcher = AsyncMock()
    dispatcher.state.get_claim = AsyncMock(return_value=_Claim(11, "the sky is blue", "open", 0.8))
    out = await sink_mod.handle_graph_sink_claim_created({"target_id": 11}, dispatcher)
    assert out == {"graph_written": True, "claim_id": 11}
    assert merged == {"id": 11, "statement": "the sky is blue", "status": "open", "confidence": 0.8}


@pytest.mark.asyncio
async def test_sink_claim_created_missing_target_id():
    # No target_id -> KeyError caught -> graph_written False.
    dispatcher = AsyncMock()
    out = await sink_mod.handle_graph_sink_claim_created({}, dispatcher)
    assert out == {"graph_written": False}


@pytest.mark.asyncio
async def test_sink_claim_created_merge_failure(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(tools_mod, "merge_claim", _boom)
    dispatcher = AsyncMock()
    dispatcher.state.get_claim = AsyncMock(return_value=_Claim(11, "s", "open", 0.8))
    out = await sink_mod.handle_graph_sink_claim_created({"target_id": 11}, dispatcher)
    assert out == {"graph_written": False}
