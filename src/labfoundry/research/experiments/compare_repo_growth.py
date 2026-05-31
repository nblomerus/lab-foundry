"""
Experiment: compare_repo_growth.

Pull current snapshot stats for a set of GitHub repos and compute simple
derived ratios (stars/age_days, open_issues/stars, days_since_push). Useful
for "is this tech actually being adopted?" — comparing 2-5 repos head-to-head
turns vibes into numbers.

Params:
    repos : list[str]  — "owner/name" form, 1-6 repos

Result:
    {
      repos: [{
        owner, name, stars, forks, open_issues, watchers,
        created_at, pushed_at, age_days, days_since_push,
        stars_per_day_avg, issues_per_star_ratio, html_url
      }],
      comparison: {
        leader_by_stars: str,
        leader_by_recent_activity: str,
        oldest: str,
        newest: str,
      }
    }

Uses the unauthenticated GitHub API; rate limit is ~60 req/hour per IP, fine
for our task volume. If `GITHUB_TOKEN` is in env, we use it (5000 req/h).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx

from labfoundry.research.experiments import REGISTRY
from labfoundry.research.fetcher import HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)


GITHUB_API = "https://api.github.com"


async def _fetch_repo(client: httpx.AsyncClient, full_name: str) -> dict:
    """Fetch one repo's metadata. Raises httpx.HTTPStatusError on 404 etc."""
    resp = await client.get(f"{GITHUB_API}/repos/{full_name}")
    resp.raise_for_status()
    return resp.json()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _digest(raw: dict) -> dict:
    """Reduce GitHub's verbose response to the few fields we actually compare."""
    created = _parse_iso(raw["created_at"])
    pushed = _parse_iso(raw["pushed_at"])
    now = datetime.now(UTC)
    age_days = max(1, (now - created).days)
    days_since_push = (now - pushed).days

    stars = raw.get("stargazers_count", 0) or 0
    open_issues = raw.get("open_issues_count", 0) or 0
    return {
        "owner": raw["owner"]["login"],
        "name": raw["name"],
        "full_name": raw["full_name"],
        "html_url": raw["html_url"],
        "description": (raw.get("description") or "")[:300],
        "stars": stars,
        "forks": raw.get("forks_count", 0) or 0,
        "open_issues": open_issues,
        "watchers": raw.get("subscribers_count", 0) or 0,
        "created_at": raw["created_at"],
        "pushed_at": raw["pushed_at"],
        "age_days": age_days,
        "days_since_push": days_since_push,
        "stars_per_day_avg": round(stars / age_days, 3),
        "issues_per_star_ratio": round(open_issues / max(1, stars), 4),
        "archived": bool(raw.get("archived")),
        "language": raw.get("language"),
    }


async def run(params: dict, *, dispatcher) -> dict:
    repos = params.get("repos") or []
    if not repos:
        raise ValueError("compare_repo_growth requires `repos` (1-6 owner/name)")
    repos = [r for r in repos if isinstance(r, str) and "/" in r][:6]
    if not repos:
        raise ValueError("compare_repo_growth: no valid owner/name entries")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        digested: list[dict] = []
        for full in repos:
            try:
                raw = await _fetch_repo(client, full)
                digested.append(_digest(raw))
            except httpx.HTTPStatusError as e:
                digested.append(
                    {
                        "full_name": full,
                        "error": f"HTTP {e.response.status_code}",
                    }
                )
            except Exception as e:  # noqa: BLE001
                digested.append({"full_name": full, "error": str(e)[:200]})

    valid = [r for r in digested if "error" not in r]
    comparison: dict = {}
    if valid:
        by_stars = max(valid, key=lambda r: r["stars"])
        by_activity = min(valid, key=lambda r: r["days_since_push"])
        oldest = max(valid, key=lambda r: r["age_days"])
        newest = min(valid, key=lambda r: r["age_days"])
        comparison = {
            "leader_by_stars": by_stars["full_name"],
            "leader_by_recent_activity": by_activity["full_name"],
            "oldest": oldest["full_name"],
            "newest": newest["full_name"],
        }

    return {"repos": digested, "comparison": comparison}


REGISTRY["compare_repo_growth"] = run
