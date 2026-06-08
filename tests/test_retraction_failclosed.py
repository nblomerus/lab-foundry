"""
Fail-closed-on-unverified-retraction (the Mimir fail-open fix).

Tri-state retraction probes mean _resolve_signals must distinguish
  retracted=True            (hard-gate BLOCK)
  retraction_unverified=True (probe couldn't verify -> ingest holds the source)
  both False                 (verified clean -> admit)

Pure: the network probes are monkeypatched, so no live APIs / DB.
"""

from __future__ import annotations

import pytest

from agents.mimir import handler as H
from library.trust import DocMeta


def _aconst(val):
    async def f(*_a, **_k):
        return val

    return f


@pytest.mark.asyncio
async def test_arxiv_unverified_sets_flag(monkeypatch):
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(None))  # probe couldn't verify
    meta = DocMeta(arxiv_id="2405.00001")
    await H._resolve_signals(meta)
    assert meta.retraction_unverified is True
    assert meta.retracted is False


@pytest.mark.asyncio
async def test_arxiv_withdrawn_blocks_not_unverified(monkeypatch):
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(True))
    meta = DocMeta(arxiv_id="2405.00001")
    await H._resolve_signals(meta)
    assert meta.retracted is True
    assert meta.retraction_unverified is False


@pytest.mark.asyncio
async def test_arxiv_clean_admits(monkeypatch):
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(False))  # fetched, no withdrawal
    meta = DocMeta(arxiv_id="2405.00001")
    await H._resolve_signals(meta)
    assert meta.retracted is False
    assert meta.retraction_unverified is False


@pytest.mark.asyncio
async def test_doi_unverified_sets_flag(monkeypatch):
    monkeypatch.setattr(H, "_doi_resolves", _aconst(False))
    monkeypatch.setattr(H, "_doi_retracted", _aconst(None))  # Crossref unreachable
    meta = DocMeta(doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.retraction_unverified is True
    assert meta.retracted is False


@pytest.mark.asyncio
async def test_retracted_takes_precedence_over_unverified(monkeypatch):
    # arXiv probe can't verify (unknown) but Crossref flags a retraction -> BLOCK wins.
    monkeypatch.setattr(H, "_arxiv_withdrawn", _aconst(None))
    monkeypatch.setattr(H, "_doi_resolves", _aconst(True))
    monkeypatch.setattr(H, "_doi_retracted", _aconst(True))
    meta = DocMeta(arxiv_id="2405.00001", doi="10.1/x")
    await H._resolve_signals(meta)
    assert meta.retracted is True
    assert meta.retraction_unverified is False


def test_strict_default_on_and_overridable(monkeypatch):
    monkeypatch.delenv("MIMIR_RETRACTION_STRICT", raising=False)
    assert H._retraction_strict() is True
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "off")
    assert H._retraction_strict() is False
    monkeypatch.setenv("MIMIR_RETRACTION_STRICT", "on")
    assert H._retraction_strict() is True
