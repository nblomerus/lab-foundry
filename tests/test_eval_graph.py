"""Unit tests for eval.graph.extract_slice — the reasoning-layer vertical-slice driver
that samples N papers from Postgres, extracts each paper's concepts with ONE LLM call,
projects them into Neo4j, then measures the before/after and prints the traversals.

Everything external is mocked: Neo4j via tests._helpers.FakeNeoDriver (patched onto the
module's _get_driver), Postgres via ScriptedConn (asyncpg.connect monkeypatched), and the
per-paper LLM concept extractor (extract_paper_concepts) + the projection
(project_paper_concepts) are stubbed. No real Postgres / Neo4j / Ollama / network is touched,
and DATABASE_URL is set only via monkeypatch.setenv. The driver is exercised through its
run() happy path, the empty-sample and zero-counts branches, the traversal renderers, and
main() (with asyncio.run / argparse patched).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from eval.graph import extract_slice
from tests._helpers import FakeNeoDriver, ScriptedConn


# ── helpers ──────────────────────────────────────────────────────────────────
def _counts(**over):
    base = {
        "methods": 3,
        "datasets": 2,
        "tasks": 1,
        "uses": 5,
        "eval_on": 2,
        "addresses": 1,
        "papers_with_concepts": 2,
    }
    base.update(over)
    return base


def _papers(*rows):
    """Default two-paper sample; each row is a dict with id/title/body."""
    if rows:
        return list(rows)
    return [
        {"id": 1, "title": "Attention Is All You Need", "body": "we propose the transformer"},
        {"id": 2, "title": "A Sufficiently Long Title For The Filter", "body": "scaling laws"},
    ]


def _measure_on_run(counts, traversal_rows=None, profile=None):
    """A Neo4j on_run that returns the counts record for the _COUNTS_CYPHER query,
    traversal rows for the shared-methods query, and a profile record otherwise."""

    def on_run(query, params):
        if "AS methods" in query:
            return [counts] if counts is not None else []
        if "USES]->(m:METHOD)<-[:USES]" in query:
            return traversal_rows or []
        if "collect(type(r)" in query:
            return [profile] if profile is not None else []
        return []

    return on_run


def _patch_sample(monkeypatch, papers):
    """asyncpg.connect → a ScriptedConn whose fetch returns the sample rows."""
    conn = ScriptedConn([("FROM documents d", papers)])
    conn.close = AsyncMock()
    monkeypatch.setattr(extract_slice.asyncpg, "connect", AsyncMock(return_value=conn))
    return conn


def _patch_extraction(monkeypatch, *, written=None):
    """Stub the per-paper LLM extractor + the projection so no model/Neo4j write happens."""
    epc = AsyncMock(return_value={"methods": [{"key": "m", "name": "M"}], "datasets": [], "tasks": []})
    monkeypatch.setattr(extract_slice, "extract_paper_concepts", epc)
    proj = AsyncMock(return_value=written or {"methods": 2, "datasets": 1, "tasks": 0})
    monkeypatch.setattr(extract_slice, "project_paper_concepts", proj)
    monkeypatch.setattr(extract_slice, "ensure_concept_constraints", AsyncMock())
    return epc, proj


# ── run(): happy path ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_happy_wires_pipeline_and_prints(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    driver = FakeNeoDriver(
        _measure_on_run(
            _counts(),
            traversal_rows=[{"method": "attention", "papers": 4}],
            profile={"id": 1, "concepts": ["USES:Attention", "EVALUATED_ON:GLUE"]},
        )
    )
    monkeypatch.setattr(extract_slice, "_get_driver", AsyncMock(return_value=driver))
    conn = _patch_sample(monkeypatch, _papers())
    epc, proj = _patch_extraction(monkeypatch)

    await extract_slice.run(n=2, seed=7, model=None)
    out = capsys.readouterr().out

    # measure printed before + after, plus per-paper projection lines.
    assert "BEFORE:" in out
    assert "AFTER:" in out
    assert "[1/2] paper 1" in out and "[2/2] paper 2" in out
    assert "+2m +1d +0t" in out
    # totals accumulate across the 2 papers (2*2 methods, 2*1 datasets).
    assert "projected this run: {'methods': 4, 'datasets': 2, 'tasks': 0}" in out
    # coverage uses after.papers_with_concepts / len(papers).
    assert "coverage: 2/2 sampled papers now have" in out
    # traversal renders the shared-method row and the profile record.
    assert "shared by ~4 papers" in out
    assert "paper 1:" in out
    # wiring: constraints ensured, model defaulted (no model kwarg threaded), sample closed.
    extract_slice.ensure_concept_constraints.assert_awaited_once()
    assert epc.await_count == 2
    assert "model" not in epc.await_args.kwargs  # no override → default model
    assert proj.await_count == 2
    conn.close.assert_awaited()


@pytest.mark.asyncio
async def test_run_threads_model_override(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    driver = FakeNeoDriver(_measure_on_run(_counts()))
    monkeypatch.setattr(extract_slice, "_get_driver", AsyncMock(return_value=driver))
    _patch_sample(monkeypatch, _papers())
    epc, _ = _patch_extraction(monkeypatch)

    await extract_slice.run(n=2, seed=1, model="qwen2.5:14b")
    out = capsys.readouterr().out
    assert "model=qwen2.5:14b" not in out  # printed via log.info, not stdout
    # the model override is threaded into the extractor as a kwarg.
    assert epc.await_args.kwargs.get("model") == "qwen2.5:14b"


# ── run(): empty-sample branch ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_empty_sample(monkeypatch, capsys):
    """No papers match the filter → no extraction loop, totals stay zero, coverage 0/1 guard."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    driver = FakeNeoDriver(_measure_on_run(_counts(papers_with_concepts=0)))
    monkeypatch.setattr(extract_slice, "_get_driver", AsyncMock(return_value=driver))
    _patch_sample(monkeypatch, [])
    epc, proj = _patch_extraction(monkeypatch)

    await extract_slice.run(n=5, seed=3, model=None)
    out = capsys.readouterr().out

    epc.assert_not_awaited()
    proj.assert_not_awaited()
    assert "projected this run: {'methods': 0, 'datasets': 0, 'tasks': 0}" in out
    # max(len(papers), 1) guard avoids ZeroDivision → 0/0 sampled, 0%.
    assert "coverage: 0/0 sampled papers now have ≥1 concept (0%)" in out


# ── run(): zero-counts / empty-traversal branches ──────────────────────────────
@pytest.mark.asyncio
async def test_run_zero_counts_and_no_traversals(monkeypatch, capsys):
    """_measure returns {} (single() None) and the traversals find nothing → fallback lines."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    # counts=None → single() returns None → _measure returns {} → cov defaults to 0.
    driver = FakeNeoDriver(_measure_on_run(None, traversal_rows=[], profile=None))
    monkeypatch.setattr(extract_slice, "_get_driver", AsyncMock(return_value=driver))
    _patch_sample(monkeypatch, _papers())
    _patch_extraction(monkeypatch, written={"methods": 0, "datasets": 0, "tasks": 0})

    await extract_slice.run(n=2, seed=7, model=None)
    out = capsys.readouterr().out

    assert "BEFORE: {}" in out and "AFTER:  {}" in out
    assert "coverage: 0/2 sampled papers now have ≥1 concept (0%)" in out
    # empty shared-methods rows → the "(no shared methods yet ...)" fallback.
    assert "(no shared methods yet" in out
    # profile None → the profile-record line (id + concepts list) is not printed.
    assert "[" not in out.split("Profile")[-1]


# ── _measure (pure-ish, FakeNeoDriver) ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_measure_returns_record_dict():
    driver = FakeNeoDriver(_measure_on_run(_counts(methods=9)))
    out = await extract_slice._measure(driver)
    assert out["methods"] == 9
    assert out["papers_with_concepts"] == 2


@pytest.mark.asyncio
async def test_measure_empty_when_no_record():
    driver = FakeNeoDriver(lambda q, p: [])
    assert await extract_slice._measure(driver) == {}


# ── _sample (ScriptedConn) ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sample_returns_rows_and_binds_seed_and_limit():
    conn = ScriptedConn([("FROM documents d", _papers())])
    rows = await extract_slice._sample(conn, n=2, seed=7)
    assert [r["id"] for r in rows] == [1, 2]
    # seed is stringified and passed as $1, n as $2.
    kind, sql, args = conn.calls[0]
    assert kind == "fetch"
    assert args == ("7", 2)


@pytest.mark.asyncio
async def test_sample_empty():
    conn = ScriptedConn([("FROM documents d", [])])
    assert await extract_slice._sample(conn, n=3, seed=1) == []


# ── _traversals branches ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_traversals_with_rows_and_profile(capsys):
    driver = FakeNeoDriver(
        _measure_on_run(
            _counts(),
            traversal_rows=[{"method": "attention", "papers": 4}, {"method": "rag", "papers": 2}],
            profile={"id": 7, "concepts": ["USES:Attention"]},
        )
    )
    await extract_slice._traversals(driver)
    out = capsys.readouterr().out
    assert "'attention': shared by ~4 papers" in out
    assert "'rag': shared by ~2 papers" in out
    assert "paper 7: ['USES:Attention']" in out


@pytest.mark.asyncio
async def test_traversals_empty(capsys):
    driver = FakeNeoDriver(_measure_on_run(_counts(), traversal_rows=[], profile=None))
    await extract_slice._traversals(driver)
    out = capsys.readouterr().out
    assert "(no shared methods yet" in out
    assert "paper " not in out  # no profile record → no profile line


# ── main(): argparse + asyncio.run patched ──────────────────────────────────────
def test_main_invokes_run_via_asyncio(monkeypatch):
    seen = {}

    def _run(coro):
        coro.close()  # never actually await the real run()
        return None

    monkeypatch.setattr(asyncio, "run", _run)

    def _fake_run(n, seed, model):
        seen.update(n=n, seed=seed, model=model)

        async def _c():
            return None

        return _c()

    monkeypatch.setattr(extract_slice, "run", _fake_run)
    monkeypatch.setattr(sys, "argv", ["eval.graph.extract_slice", "--n", "3", "--seed", "11", "--model", "m"])

    extract_slice.main()
    assert seen == {"n": 3, "seed": 11, "model": "m"}


def test_main_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), None)[1])

    def _parse(self):
        captured["ns"] = argparse.Namespace(n=12, seed=7, model=None)
        return captured["ns"]

    async def _coro(*a, **k):
        return None

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _parse)
    monkeypatch.setattr(extract_slice, "run", _coro)

    extract_slice.main()
    assert captured["ns"].n == 12 and captured["ns"].seed == 7 and captured["ns"].model is None
