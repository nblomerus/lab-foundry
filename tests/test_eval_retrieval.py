"""Tests for eval/retrieval/evaluate.py — the known-item retrieval eval harness.

Fully mocked: no live corpus, no Ollama, no asyncpg. The pure query/metric helpers
are exercised directly; the async paths (build_goldset, run_eval) have their I/O
seams patched — asyncpg.connect, corpus_search, and the gold-set file.
"""

from __future__ import annotations

import json
import sys

import pytest

import eval.retrieval.evaluate as ev
import library.corpus.tools as corpus_tools


class _Chunk:
    """A corpus_search result row — run_eval only reads `.document_id`."""

    def __init__(self, document_id: int):
        self.document_id = document_id


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    async def fetch(self, sql, *args):
        return self._rows

    async def close(self):
        self.closed = True


# ── pure: query construction ─────────────────────────────────────────────────


def test_best_sentence_picks_in_range_sentence():
    text = "Short. This sentence has more than eight words so it qualifies nicely here. Tiny."
    assert ev._best_sentence(text) == "This sentence has more than eight words so it qualifies nicely here."


def test_best_sentence_none_when_all_out_of_range():
    assert ev._best_sentence("Too short.") is None
    assert ev._best_sentence("") is None
    assert ev._best_sentence(None) is None


def test_best_sentence_skips_markdown_heading():
    # A "[#..." sentence in range is skipped; the next in-range one wins.
    text = (
        "[# heading line with enough words to be in the eight to thirty-two band here]. "
        "A genuine sentence of sufficient length to be selected as the passage probe."
    )
    got = ev._best_sentence(text)
    assert got is not None
    assert got.startswith("A genuine sentence")


def test_distinctive_term_prefers_hyphenated_or_acronym():
    assert ev._distinctive_term("Retrieval-Augmented generation for question answering") == "Retrieval-Augmented"
    assert ev._distinctive_term("A study of BERT and friends") == "BERT"


def test_distinctive_term_camelcase():
    assert ev._distinctive_term("the PyTorch ecosystem") == "PyTorch"


def test_distinctive_term_longest_token_fallback():
    # No special tokens -> longest non-stopword alphabetic token (>=6 chars).
    assert ev._distinctive_term("the quantization roadmap") == "quantization"


def test_distinctive_term_none_when_nothing_usable():
    assert ev._distinctive_term("") is None
    assert ev._distinctive_term("a of the to in on") is None
    assert ev._distinctive_term("cat dog") is None  # both < 6 chars and not special


def test_build_queries_full_set():
    q = ev._build_queries(
        "Retrieval-Augmented Generation Survey",
        "This is a passage sentence with plenty of words to clear the lower bound nicely indeed.",
    )
    assert q["title"] == "Retrieval-Augmented Generation Survey"
    assert "passage" in q
    assert q["lexical"] == "Retrieval-Augmented"


def test_build_queries_drops_short_title():
    q = ev._build_queries("Tiny", "no usable sentence here.")
    assert "title" not in q


# ── pure: metrics ────────────────────────────────────────────────────────────


def test_doc_rank_found_with_dedup():
    chunks = [_Chunk(7), _Chunk(7), _Chunk(3), _Chunk(9)]
    assert ev._doc_rank(chunks, 3) == 2  # 7 dedupes to rank 1, 3 is rank 2


def test_doc_rank_first_position():
    assert ev._doc_rank([_Chunk(5), _Chunk(6)], 5) == 1


def test_doc_rank_not_found():
    assert ev._doc_rank([_Chunk(1), _Chunk(2)], 99) is None
    assert ev._doc_rank([], 1) is None


def test_ndcg_at():
    assert ev._ndcg_at(1, 10) == 1.0
    assert ev._ndcg_at(None, 10) == 0.0
    assert ev._ndcg_at(11, 10) == 0.0  # beyond cutoff
    assert ev._ndcg_at(3, 10) == pytest.approx(1.0 / 2.0)  # 1/log2(4)=0.5


# ── _load_dotenv ─────────────────────────────────────────────────────────────


def test_load_dotenv_noop_when_database_url_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev, "REPO_ROOT", tmp_path)
    ev._load_dotenv()  # returns early, no crash even though no .env exists


def test_load_dotenv_noop_when_no_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(ev, "REPO_ROOT", tmp_path)  # empty dir, no .env
    ev._load_dotenv()  # no crash


def test_load_dotenv_loads_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text('# a comment\nEVAL_PROBE_KEY="val-1"\nnot_a_kv_line\n')
    monkeypatch.setattr(ev, "REPO_ROOT", tmp_path)
    try:
        ev._load_dotenv()
        assert ev.os.environ["EVAL_PROBE_KEY"] == "val-1"
    finally:
        ev.os.environ.pop("EVAL_PROBE_KEY", None)


# ── build_goldset ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_goldset_writes_items(monkeypatch, tmp_path):
    rows = [
        {
            "id": 1,
            "title": "Retrieval-Augmented Generation for Open QA",
            "chunk_text": "A passage sentence long enough to clear the lower word bound used by the probe.",
        },
        {"id": 2, "title": "Sparse Mixture of Experts at Scale", "chunk_text": "short"},
        {"id": 3, "title": "x", "chunk_text": "irrelevant"},  # title < 12 -> no title query -> dropped
    ]

    async def _fake_connect(dsn):
        return _FakeConn(rows)

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev.asyncpg, "connect", _fake_connect)
    gp = tmp_path / "goldset.jsonl"
    monkeypatch.setattr(ev, "GOLDSET_PATH", gp)

    n = await ev.build_goldset(n=10, seed=7)
    assert n == 2  # rows 1 and 2 have a title query; row 3 dropped
    written = [json.loads(line) for line in gp.read_text().splitlines()]
    assert {w["document_id"] for w in written} == {1, 2}
    assert "title" in written[0]["queries"]


@pytest.mark.asyncio
async def test_build_goldset_respects_n_cap(monkeypatch, tmp_path):
    rows = [{"id": i, "title": f"A Sufficiently Long Title Number {i}", "chunk_text": "x"} for i in range(10)]

    async def _fake_connect(dsn):
        return _FakeConn(rows)

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev.asyncpg, "connect", _fake_connect)
    monkeypatch.setattr(ev, "GOLDSET_PATH", tmp_path / "g.jsonl")

    n = await ev.build_goldset(n=3, seed=1)
    assert n == 3  # capped


# ── run_eval ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_eval_raises_without_goldset(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev, "GOLDSET_PATH", tmp_path / "nope.jsonl")
    with pytest.raises(SystemExit):
        await ev.run_eval(k=20, min_trust=None)


def _write_goldset(tmp_path, items):
    gp = tmp_path / "goldset.jsonl"
    gp.write_text("\n".join(json.dumps(it) for it in items) + "\n")
    return gp


@pytest.mark.asyncio
async def test_run_eval_hits(monkeypatch, tmp_path, capsys):
    items = [{"document_id": 42, "title": "Target Paper", "queries": {"title": "Target Paper"}}]
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev, "GOLDSET_PATH", _write_goldset(tmp_path, items))

    async def _search(query, k, min_trust, hybrid):
        return [_Chunk(42)]  # always retrieves the target at rank 1

    monkeypatch.setattr(corpus_tools, "corpus_search", _search)
    await ev.run_eval(k=10, min_trust=None, mode="hybrid")
    out = capsys.readouterr().out
    assert "RETRIEVAL EVAL" in out
    assert "R@1=1.00" in out


@pytest.mark.asyncio
async def test_run_eval_misses_records_failures(monkeypatch, tmp_path, capsys):
    items = [{"document_id": 42, "title": "Target", "queries": {"title": "Target", "lexical": "Target"}}]
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(ev, "GOLDSET_PATH", _write_goldset(tmp_path, items))

    async def _search(query, k, min_trust, hybrid):
        return [_Chunk(1), _Chunk(2)]  # target 42 never present -> all misses

    monkeypatch.setattr(corpus_tools, "corpus_search", _search)
    await ev.run_eval(k=5, min_trust="B", mode="dense")
    out = capsys.readouterr().out
    assert "NOT-FOUND" in out
    assert "dense-only" in out


# ── _report ──────────────────────────────────────────────────────────────────


def test_report_handles_empty_and_full(capsys):
    ranks = {"title": [1, 2, None], "passage": [], "lexical": [None, None]}
    ev._report(ranks, k=20, failures=[("lexical", "q", "a title")], mode="hybrid")
    out = capsys.readouterr().out
    assert "OVERALL" in out
    assert "MRR=" in out
    assert "NOT-FOUND" in out


def test_report_no_failures_branch(capsys):
    ev._report({"title": [1, 1]}, k=10, failures=[], mode="hybrid")
    out = capsys.readouterr().out
    assert "OVERALL" in out
    assert "NOT-FOUND" not in out


# ── main / CLI ───────────────────────────────────────────────────────────────


def _patch_run(monkeypatch, ret=3):
    def _fake_run(coro):
        coro.close()  # avoid 'coroutine was never awaited'
        return ret

    monkeypatch.setattr(ev.asyncio, "run", _fake_run)


def test_main_build(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "build", "--n", "3", "--seed", "1"])
    _patch_run(monkeypatch, ret=3)
    ev.main()
    assert "gold set: 3 documents" in capsys.readouterr().out


def test_main_run(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "run", "--k", "5", "--mode", "dense"])
    _patch_run(monkeypatch)
    ev.main()  # no crash; asyncio.run stubbed
