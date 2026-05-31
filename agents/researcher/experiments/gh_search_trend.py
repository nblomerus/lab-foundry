"""
Experiment: gh_search_trend.

Probe GitHub for *adoption inflection*. For each query, count repositories
created in two adjacent time windows and report the ratio (recent / prior).
A ratio > 1.5 with a non-trivial absolute count is a strong "people are
actually building this right now" signal that fills the gap between
`count_demand_signal` (forum chatter) and `compare_repo_growth` (specific
named repos).

Params:
    queries     : list[str]   — required; 1-5 GitHub search queries. May use
                                 GitHub search qualifiers (e.g. "language:python
                                 vector database").
    months_back : int = 6      — size of each window; must be 1-12.

Result:
    {
      windows: {"recent": "2025-11-28..2026-05-28", "prior": "2025-05-28..2025-11-28"},
      queries: [
        {
          query: str,
          recent_count: int,
          prior_count: int,
          ratio: float | null,        # recent/prior, null if prior_count==0
          top_recent: [{full_name, html_url, stars, created_at, description}]
        }
      ],
      summary: {"hottest_query": str, "growth_ratio": float}
    }

Uses the unauthenticated GitHub Search API; rate limit is ~10 req/min per IP
without a token, ~30 with one. Two calls per query keeps small queries cheap.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from agents.researcher.experiments import REGISTRY
from library.ingest.fetcher import HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _fmt_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


async def _count_and_top(
    client: httpx.AsyncClient, query: str, start: datetime, end: datetime, top: int = 4
) -> tuple[int, list[dict]]:
    """Hit /search/repositories with created:start..end. Returns (total, top hits)."""
    q = f"{query} created:{_fmt_date(start)}..{_fmt_date(end)}"
    resp = await client.get(
        f"{GITHUB_API}/search/repositories",
        params={"q": q, "sort": "stars", "order": "desc", "per_page": top, "page": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    total = int(data.get("total_count", 0))
    hits = [
        {
            "full_name": it["full_name"],
            "html_url": it["html_url"],
            "stars": int(it.get("stargazers_count", 0) or 0),
            "created_at": it.get("created_at"),
            "language": it.get("language"),
            "description": (it.get("description") or "")[:200],
        }
        for it in data.get("items", [])[:top]
    ]
    return total, hits


async def run(params: dict, *, dispatcher) -> dict:
    queries = params.get("queries") or []
    if not isinstance(queries, list) or not queries:
        raise ValueError("gh_search_trend requires `queries` (list of 1-5 strings)")
    queries = [q for q in queries if isinstance(q, str) and q.strip()][:5]
    if not queries:
        raise ValueError("gh_search_trend: all queries empty")

    months_back = int(params.get("months_back") or 6)
    months_back = max(1, min(12, months_back))

    now = datetime.now(UTC)
    recent_start = now - timedelta(days=30 * months_back)
    prior_start = now - timedelta(days=30 * months_back * 2)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    out_queries = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        # Sequential to stay friendly to the rate limit; small N.
        for q in queries:
            try:
                recent_count, top_recent = await _count_and_top(
                    client,
                    q,
                    recent_start,
                    now,
                    top=4,
                )
                prior_count, _ = await _count_and_top(
                    client,
                    q,
                    prior_start,
                    recent_start,
                    top=0,
                )
            except httpx.HTTPStatusError as e:
                out_queries.append({"query": q, "error": f"HTTP {e.response.status_code}"})
                continue
            except Exception as e:  # noqa: BLE001
                out_queries.append({"query": q, "error": str(e)[:200]})
                continue

            ratio = None
            if prior_count > 0:
                ratio = round(recent_count / prior_count, 2)
            out_queries.append(
                {
                    "query": q,
                    "recent_count": recent_count,
                    "prior_count": prior_count,
                    "ratio": ratio,
                    "top_recent": top_recent,
                }
            )

    # Summary: which query saw the biggest jump (by ratio, then by absolute).
    valid = [q for q in out_queries if "error" not in q]
    summary: dict = {}
    if valid:
        # Prefer queries with meaningful absolute volume so a 1->5 (5x) doesn't
        # beat a 200->600 (3x).
        scored = [
            (q.get("ratio") or 0, q.get("recent_count", 0), q["query"]) for q in valid if q.get("ratio") is not None
        ]
        if scored:
            # Sort by (ratio * sqrt(recent_count)) — combines growth + scale.
            import math

            scored.sort(key=lambda t: t[0] * math.sqrt(max(1, t[1])), reverse=True)
            top_ratio, top_n, top_q = scored[0]
            summary = {
                "hottest_query": top_q,
                "growth_ratio": top_ratio,
                "recent_count": top_n,
            }

    return {
        "windows": {
            "recent": f"{_fmt_date(recent_start)}..{_fmt_date(now)}",
            "prior": f"{_fmt_date(prior_start)}..{_fmt_date(recent_start)}",
            "months_back": months_back,
        },
        "queries": out_queries,
        "summary": summary,
    }


REGISTRY["gh_search_trend"] = run
