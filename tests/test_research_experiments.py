"""
Tests for the experiment dispatcher and the three pure-Python kinds.

`fetch_pricing` runs an LLM call so we don't exercise it end-to-end here.
The other two — `count_demand_signal` and `compare_repo_growth` — are pure
code that hits external HTTP, and we patch the network at the call site.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from labfoundry.research.experiments import (
    REGISTRY,
    UnknownExperiment,
    dispatch,
)


class _Dispatcher:
    """Trivial dispatcher stand-in. Experiments that don't reach the router
    only touch dispatcher.state, which can be None for these pure runners."""

    def __init__(self):
        self.state = None


@pytest.mark.asyncio
async def test_registry_has_all_kinds():
    assert set(REGISTRY.keys()) == {
        "fetch_pricing",
        "count_demand_signal",
        "compare_repo_growth",
        "gh_search_trend",
    }


@pytest.mark.asyncio
async def test_dispatch_unknown_raises():
    with pytest.raises(UnknownExperiment):
        await dispatch("nope", {}, dispatcher=_Dispatcher())


# --------------------------------------------------------------------------
# count_demand_signal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_demand_signal_aggregates(monkeypatch):
    from labfoundry.mcp_servers.labfoundry_research.tools import SearchResult
    from labfoundry.research.experiments import count_demand_signal as cds

    async def _fake_reddit(query, limit, **_):
        # Return a different shape per query so counts are distinct.
        if "ai" in query.lower():
            return [SearchResult(title=f"r/x {i}", url=f"https://r/{i}", snippet="s", source="reddit") for i in range(7)]
        return [SearchResult(title="x", url="https://r/0", snippet="s", source="reddit")]

    async def _fake_hn(query, limit, **_):
        return [SearchResult(title="hn", url="https://hn/1", snippet="s", source="hacker_news") for _ in range(3)]

    # _SOURCE_TOOLS is built at import time with bound references, so we
    # patch the dict itself rather than the names in the module namespace.
    monkeypatch.setitem(cds._SOURCE_TOOLS, "reddit", _fake_reddit)
    monkeypatch.setitem(cds._SOURCE_TOOLS, "hacker_news", _fake_hn)

    result = await dispatch(
        "count_demand_signal",
        {"phrases": ["AI agents broken", "labfoundry tools"], "sources": ["reddit", "hacker_news"]},
        dispatcher=_Dispatcher(),
    )
    assert result["counts"]["AI agents broken"]["reddit"] == 7
    assert result["counts"]["AI agents broken"]["hacker_news"] == 3
    assert result["counts"]["labfoundry tools"]["reddit"] == 1
    assert result["totals_per_phrase"]["AI agents broken"] == 10
    assert result["grand_total"] == 14
    assert len(result["exemplars"]) > 0


@pytest.mark.asyncio
async def test_count_demand_signal_requires_phrases():
    with pytest.raises(ValueError):
        await dispatch("count_demand_signal", {}, dispatcher=_Dispatcher())


# --------------------------------------------------------------------------
# compare_repo_growth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_repo_growth_digests_and_ranks():
    fake = {
        "owner": {"login": "anthropic"},
        "name": "claude-code",
        "full_name": "anthropic/claude-code",
        "html_url": "https://github.com/anthropic/claude-code",
        "description": "agentic CLI",
        "stargazers_count": 12000,
        "forks_count": 800,
        "open_issues_count": 240,
        "subscribers_count": 90,
        "created_at": "2024-01-01T00:00:00Z",
        "pushed_at": "2026-05-20T00:00:00Z",
        "archived": False,
        "language": "TypeScript",
    }
    other = {
        **fake,
        "owner": {"login": "openai"},
        "name": "codex",
        "full_name": "openai/codex",
        "html_url": "https://github.com/openai/codex",
        "stargazers_count": 6000,
        "pushed_at": "2025-12-01T00:00:00Z",
    }

    async def _fake_get(self, url):
        if "claude-code" in url:
            return httpx.Response(200, json=fake, request=httpx.Request("GET", url))
        return httpx.Response(200, json=other, request=httpx.Request("GET", url))

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        result = await dispatch(
            "compare_repo_growth",
            {"repos": ["anthropic/claude-code", "openai/codex"]},
            dispatcher=_Dispatcher(),
        )

    assert len(result["repos"]) == 2
    assert result["comparison"]["leader_by_stars"] == "anthropic/claude-code"
    # claude-code is more recently pushed -> wins recent_activity
    assert result["comparison"]["leader_by_recent_activity"] == "anthropic/claude-code"
    for r in result["repos"]:
        assert "stars_per_day_avg" in r
        assert "issues_per_star_ratio" in r


@pytest.mark.asyncio
async def test_compare_repo_growth_handles_404():
    async def _fake_get(self, url):
        return httpx.Response(404, json={"message": "Not Found"}, request=httpx.Request("GET", url))

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        result = await dispatch(
            "compare_repo_growth",
            {"repos": ["no/such-repo"]},
            dispatcher=_Dispatcher(),
        )
    assert "error" in result["repos"][0]
    assert result["comparison"] == {}


@pytest.mark.asyncio
async def test_compare_repo_growth_requires_repos():
    with pytest.raises(ValueError):
        await dispatch("compare_repo_growth", {"repos": []}, dispatcher=_Dispatcher())


# --------------------------------------------------------------------------
# Param shape tolerance — the planner is allowed to be sloppy
# --------------------------------------------------------------------------


def test_fetch_pricing_coerces_plural_companies():
    """Planner sometimes sends {"companies": [...]} instead of singular form;
    the runner accepts both."""
    from labfoundry.research.experiments.fetch_pricing import _coerce_targets

    targets = _coerce_targets({"companies": ["OpenAI", "Brave"]})
    assert targets == [{"company": "OpenAI"}, {"company": "Brave"}]


def test_fetch_pricing_coerces_plural_urls():
    from labfoundry.research.experiments.fetch_pricing import _coerce_targets

    targets = _coerce_targets({"urls": ["https://a/", "https://b/"]})
    assert targets == [{"url": "https://a/"}, {"url": "https://b/"}]


def test_fetch_pricing_singular_still_works():
    from labfoundry.research.experiments.fetch_pricing import _coerce_targets

    assert _coerce_targets({"url": "https://x/"}) == [{"url": "https://x/"}]
    assert _coerce_targets({"company": "X"}) == [{"company": "X"}]
    assert _coerce_targets({"product": "Y"}) == [{"company": "Y"}]


def test_count_demand_signal_aliases_hn():
    """The planner sometimes uses "hn" instead of "hacker_news"."""
    from labfoundry.research.experiments.count_demand_signal import _canonicalize_sources

    assert _canonicalize_sources(["reddit", "hn"]) == ["reddit", "hacker_news"]
    assert _canonicalize_sources(["HackerNews"]) == ["hacker_news"]
    assert _canonicalize_sources(["bogus"]) == []
    # No duplicates
    assert _canonicalize_sources(["hn", "hacker_news"]) == ["hacker_news"]


# --------------------------------------------------------------------------
# gh_search_trend
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_search_trend_compares_windows():
    """Two windows per query: recent and prior. The recent total in our fake
    is higher, so the ratio is > 1 and the query shows up as the hottest."""
    call_count = {"n": 0}

    async def _fake_get(self, url, params=None, **_):
        call_count["n"] += 1
        # Alternate: first call (recent) returns 120, second (prior) returns 30
        recent = call_count["n"] % 2 == 1
        total = 120 if recent else 30
        items = (
            [
                {
                    "full_name": "octocat/hello",
                    "html_url": "https://github.com/octocat/hello",
                    "stargazers_count": 42,
                    "created_at": "2026-04-01T00:00:00Z",
                    "language": "Python",
                    "description": "Fast vector DB.",
                }
            ]
            if recent
            else []
        )
        return httpx.Response(
            200,
            json={"total_count": total, "items": items},
            request=httpx.Request("GET", "https://api.github.com/search/repositories"),
        )

    with patch.object(httpx.AsyncClient, "get", new=_fake_get):
        result = await dispatch(
            "gh_search_trend",
            {"queries": ["vector database language:python"], "months_back": 3},
            dispatcher=_Dispatcher(),
        )

    q = result["queries"][0]
    assert q["recent_count"] == 120
    assert q["prior_count"] == 30
    assert q["ratio"] == 4.0
    assert len(q["top_recent"]) == 1
    assert q["top_recent"][0]["full_name"] == "octocat/hello"
    assert result["summary"]["hottest_query"] == "vector database language:python"


@pytest.mark.asyncio
async def test_gh_search_trend_requires_queries():
    with pytest.raises(ValueError):
        await dispatch("gh_search_trend", {}, dispatcher=_Dispatcher())
    with pytest.raises(ValueError):
        await dispatch("gh_search_trend", {"queries": [""]}, dispatcher=_Dispatcher())
