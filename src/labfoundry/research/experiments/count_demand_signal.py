"""
Experiment: count_demand_signal.

Hit Reddit + Hacker News search for each phrase, count matches, and return
top exemplars. Tests "are people actually complaining about / asking for
this?" with a number rather than vibes.

Params:
    phrases : list[str]                 — required, 1-6 phrases
    sources : list["reddit"|"hacker_news"]  — default ["reddit", "hacker_news"]
    limit_per_phrase : int = 25         — cap per (phrase, source) for counting
    exemplar_count : int = 3            — top-N exemplar posts to include

Result:
    {
      counts: {phrase: {source: int}},
      totals: {phrase: int, source: int},
      grand_total: int,
      exemplars: [{phrase, source, title, url, snippet}]
    }
"""

from __future__ import annotations

import asyncio
import logging

from labfoundry.mcp_servers.labfoundry_research.tools import (
    search_hacker_news,
    search_reddit,
)
from labfoundry.research.experiments import REGISTRY

log = logging.getLogger(__name__)


_SOURCE_TOOLS = {
    "reddit": search_reddit,
    "hacker_news": search_hacker_news,
}

# The planner sometimes shortens names ("hn", "hackernews"). Map common
# variants to the canonical keys so a small spelling drift doesn't drop a
# whole source from the run.
_SOURCE_ALIASES = {
    "hn": "hacker_news",
    "hackernews": "hacker_news",
    "hacker-news": "hacker_news",
    "ycombinator": "hacker_news",
    "y_combinator": "hacker_news",
    "r": "reddit",
}


def _canonicalize_sources(raw: list[str]) -> list[str]:
    """Normalize incoming source names to the canonical keys."""
    out: list[str] = []
    for s in raw or []:
        if not isinstance(s, str):
            continue
        key = s.strip().lower().replace(" ", "_")
        key = _SOURCE_ALIASES.get(key, key)
        if key in _SOURCE_TOOLS and key not in out:
            out.append(key)
    return out


async def run(params: dict, *, dispatcher) -> dict:
    phrases = params.get("phrases") or []
    if not phrases:
        raise ValueError("count_demand_signal requires `phrases` (1-6)")
    if not isinstance(phrases, list):
        raise ValueError("`phrases` must be a list")
    phrases = phrases[:6]

    sources = _canonicalize_sources(params.get("sources") or ["reddit", "hacker_news"])[:2]
    if not sources:
        raise ValueError(
            "count_demand_signal: no valid sources after aliasing. "
            f"Got: {params.get('sources')!r}. Valid: reddit | hacker_news (aliases: hn, hackernews)"
        )

    limit_per_phrase = min(int(params.get("limit_per_phrase") or 25), 50)
    exemplar_count = min(int(params.get("exemplar_count") or 3), 6)

    # Fire all (phrase, source) queries in parallel; fail-soft per pair.
    async def _one(phrase: str, source: str):
        tool = _SOURCE_TOOLS[source]
        try:
            hits = await tool(query=phrase, limit=limit_per_phrase)
        except Exception as e:  # noqa: BLE001
            log.warning("demand-signal %s/%s failed: %s", source, phrase, e)
            return phrase, source, []
        return phrase, source, hits

    pairs = [(p, s) for p in phrases for s in sources]
    raw = await asyncio.gather(*[_one(p, s) for p, s in pairs])

    counts: dict[str, dict[str, int]] = {p: {s: 0 for s in sources} for p in phrases}
    exemplar_pool: list[dict] = []

    for phrase, source, hits in raw:
        counts[phrase][source] = len(hits)
        # Take top exemplars from each phrase+source so the result is
        # representative across the matrix, not dominated by one cell.
        for hit in hits[:exemplar_count]:
            exemplar_pool.append(
                {
                    "phrase": phrase,
                    "source": source,
                    "title": hit.title,
                    "url": hit.url,
                    "snippet": hit.snippet[:240],
                }
            )

    totals_per_phrase = {p: sum(counts[p].values()) for p in phrases}
    totals_per_source = {s: sum(counts[p][s] for p in phrases) for s in sources}
    grand_total = sum(totals_per_phrase.values())

    return {
        "counts": counts,
        "totals_per_phrase": totals_per_phrase,
        "totals_per_source": totals_per_source,
        "grand_total": grand_total,
        "exemplars": exemplar_pool[: exemplar_count * len(phrases)],
        "params_used": {
            "phrases": phrases,
            "sources": sources,
            "limit_per_phrase": limit_per_phrase,
        },
    }


REGISTRY["count_demand_signal"] = run
