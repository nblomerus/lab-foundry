"""
Tests for the arXiv fetcher (`search_arxiv`) and the arXiv scout
(`scout_arxiv`).

NO real network: the httpx GET is patched to return a small canned Atom XML
feed. The point is to prove the Atom parser pulls the right fields and that the
scout turns results into SourceDescriptors with the locked
kind/source_kind/canonical_key shape — not to exercise httpx or arXiv itself.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from library.ingest import fetcher
from library.ingest.fetcher import ArxivResult, search_arxiv
from library.ingest.scouts import SourceDescriptor, scout_arxiv

# A trimmed-but-faithful arXiv Atom feed with two entries. Namespaces and
# element shapes match what export.arxiv.org actually returns.
CANNED_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <published>2024-01-02T18:00:00Z</published>
    <title>Retrieval-Augmented Generation for Knowledge Tasks</title>
    <summary>
      We study retrieval augmented generation and show
      improvements on knowledge-intensive benchmarks.
    </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <link href="http://arxiv.org/abs/2401.01234v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.01234v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.IR"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2402.05678v1</id>
    <published>2024-02-09T09:30:00Z</published>
    <title>A Second Paper With Missing Authors</title>
    <summary>An abstract with no listed authors.</summary>
    <link title="pdf" href="http://arxiv.org/pdf/2402.05678v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="stat.ML"/>
    <category term="stat.ML"/>
  </entry>
</feed>
"""


def _atom_resp() -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=CANNED_ATOM.encode("utf-8"),
        headers={"content-type": "application/atom+xml"},
        request=httpx.Request("GET", fetcher.ARXIV_API_URL),
    )


# --------------------------------------------------------------------------
# search_arxiv — Atom parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_arxiv_parses_fields():
    async def _fake_get(self, url):
        # The query must reach the arXiv API URL.
        assert url.startswith(fetcher.ARXIV_API_URL)
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        results = await search_arxiv("all:retrieval augmented generation", max_results=5)

    assert len(results) == 2
    first = results[0]
    assert isinstance(first, ArxivResult)
    assert first.arxiv_id == "2401.01234"  # version-stripped canonical id
    assert first.title == "Retrieval-Augmented Generation for Knowledge Tasks"
    assert first.authors == ["Ada Lovelace", "Alan Turing"]
    assert "retrieval augmented generation" in first.abstract.lower()
    assert first.pdf_url == "http://arxiv.org/pdf/2401.01234v2"
    assert "cs.CL" in first.categories and "cs.IR" in first.categories
    assert first.published is not None
    assert first.published.isoformat() == "2024-01-02"


@pytest.mark.asyncio
async def test_search_arxiv_sort_defaults_to_date_and_honors_relevance():
    """The `sort` arg maps to arXiv's sortBy: default newest-first (standing sweep),
    'relevance' for targeted searches (so a niche query gets on-topic, not newest arXiv-wide)."""
    urls: list[str] = []

    async def _fake_get(self, url):
        urls.append(url)
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        await search_arxiv("all:gaussian process kernel design")  # default
        await search_arxiv("all:gaussian process kernel design", sort="relevance")

    assert "sortBy=submittedDate" in urls[0]
    assert "sortBy=relevance" in urls[1]


@pytest.mark.asyncio
async def test_scout_arxiv_threads_sort_to_api():
    urls: list[str] = []

    async def _fake_get(self, url):
        urls.append(url)
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        await scout_arxiv(["niche topic"], per_topic=5, sort="relevance")

    assert urls and "sortBy=relevance" in urls[0]


@pytest.mark.asyncio
async def test_search_arxiv_robust_to_missing_authors():
    async def _fake_get(self, url):
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        results = await search_arxiv("anything")

    second = results[1]
    assert second.arxiv_id == "2402.05678"  # version-stripped canonical id
    assert second.authors == []  # no <author> elements -> empty, not crash
    assert second.categories == ["stat.ML"]


@pytest.mark.asyncio
async def test_search_arxiv_transport_error_returns_empty():
    async def _fake_get(self, url):
        raise httpx.ConnectError("nope")

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        results = await search_arxiv("whatever")

    assert results == []


@pytest.mark.asyncio
async def test_search_arxiv_bad_feed_returns_empty():
    async def _fake_get(self, url):
        return httpx.Response(
            status_code=200,
            content=b"<<<not xml at all",
            headers={"content-type": "application/atom+xml"},
            request=httpx.Request("GET", fetcher.ARXIV_API_URL),
        )

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        results = await search_arxiv("whatever")

    assert results == []


# --------------------------------------------------------------------------
# scout_arxiv — descriptor shape + dedupe
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scout_arxiv_returns_paper_descriptors():
    async def _fake_get(self, url):
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        descriptors = await scout_arxiv(["retrieval augmented generation"], per_topic=5)

    assert len(descriptors) == 2
    d = descriptors[0]
    assert isinstance(d, SourceDescriptor)
    assert d.kind == "paper"
    assert d.source_kind == "arxiv"
    assert d.canonical_key == "2401.01234"  # version-stripped canonical id
    assert d.arxiv_id == "2401.01234"  # version-stripped canonical id
    assert d.url == "http://arxiv.org/pdf/2401.01234v2"  # PDF URL retains the version
    assert d.title == "Retrieval-Augmented Generation for Knowledge Tasks"
    assert "retrieval augmented generation" in (d.why or "")


@pytest.mark.asyncio
async def test_scout_arxiv_dedupes_by_arxiv_id_across_topics():
    # Both topics return the SAME canned feed; the scout must dedupe by id.
    calls = 0

    async def _fake_get(self, url):
        nonlocal calls
        calls += 1
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        descriptors = await scout_arxiv(["topic one", "topic two"], per_topic=5)

    assert calls == 2  # one HTTP call per topic

    keys = [d.canonical_key for d in descriptors]
    assert len(keys) == len(set(keys)) == 2  # deduped, not 4


@pytest.mark.asyncio
async def test_scout_arxiv_is_pure_no_events_no_db():
    """A scout returns descriptors only — it must not require a bus or DB.
    We assert it runs with nothing but a mocked HTTP call and returns plain
    SourceDescriptor models."""

    async def _fake_get(self, url):
        return _atom_resp()

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        descriptors = await scout_arxiv(["x"])

    assert all(isinstance(d, SourceDescriptor) for d in descriptors)
