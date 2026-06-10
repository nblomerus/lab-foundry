"""
Scout bots — the Library's DISCOVERY layer (MIMIR_WARDEN_SCOPE.md §3).

The Director's design: discovery is done by MANY small, autonomous "scout"
bots — one per source (arXiv, GitHub, OpenML, …) — that look for new research
material and surface it as `source.discovered` events feeding the Librarian.
This is deliberately NOT one monolithic sweep: each scout owns exactly one
source, can be scheduled / rate-limited / disabled independently, and is small
enough to reason about and test in isolation.

THE SCOUT PATTERN (copy this for github/openml/…):

  1. A scout is a PURE async function:
         async def scout_<source>(topics, ...) -> list[SourceDescriptor]
     It calls the source's search/list API (via library.ingest.fetcher or a
     source-specific client), normalizes each hit into a `SourceDescriptor`, and
     RETURNS the list. That's it.

  2. A scout does NOT emit events and does NOT touch the DB. Event emission
     (`source.discovered`) and dedupe-against-already-ingested are the
     Librarian handler's job. Keeping scouts side-effect-free makes them
     trivially testable (no DB/bus fixture, no network — mock the fetcher) and
     lets the handler decide policy (which descriptors to act on) in one place.

  3. Each scout sets `source_kind` to its own source name and `canonical_key`
     to that source's stable id (arXiv id, "owner/repo", OpenML dataset id, …),
     so the handler can dedupe across runs by (source_kind, canonical_key)
     without knowing the source's internals.

  4. `kind` is the ingest-side `DocumentKind` ('paper' for arXiv/GitHub-papers,
     'code' for repos, 'dataset' for OpenML, …) — it tells the Librarian which
     ingest path the descriptor belongs on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx
from pydantic import BaseModel

from library.ingest.fetcher import search_arxiv
from library.ingest.schemas import DocumentKind

log = logging.getLogger(__name__)

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8081")
_GITHUB_API = "https://api.github.com"
_HF_API = "https://huggingface.co"
_OPENML_API = "https://www.openml.org/api/v1/json"
_SCOUT_UA = "labfoundry-scout"
# Courtesy delay (seconds) between successive per-topic API calls within one
# scout run. arXiv and GitHub rate-limit aggressive bursts; an aggressive sweep
# fans over many topics, so we space the calls. Self-hosted SearXNG and the HF
# hub are generous and don't pace.
_SCOUT_TOPIC_DELAY = float(os.environ.get("SCOUT_TOPIC_DELAY", "2.5"))
# OpenML v1 has no substring search and its data_name filter is EXACT (a miss returns
# HTTP 412 "No results"), so a subfield topic matched ~nothing. We fetch one page of
# active datasets per scout run and substring-match topic tokens against their names.
_OPENML_ACTIVE_POOL = int(os.environ.get("OPENML_ACTIVE_POOL", "1000"))


# -------------------------------------------------------------------------
# SourceDescriptor — a scout's output row.
#
# A discovered candidate for ingestion, source-agnostic on purpose: the
# Librarian handler turns each descriptor into a `source.discovered` event and
# (after dedupe) an ingest job, without caring which scout produced it.
# `canonical_key` is the source's stable id (deduped per source_kind); the
# typed id fields (arxiv_id/doi) and url are best-effort enrichments the ingest
# path can use directly.
# -------------------------------------------------------------------------


class SourceDescriptor(BaseModel):
    kind: DocumentKind  # ingest taxonomy: 'paper' for arXiv
    source_kind: str  # which scout found it, e.g. 'arxiv'
    canonical_key: str  # stable per-source id; dedupe key
    url: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    title: str | None = None
    why: str | None = None  # short note on why this surfaced (e.g. topic)


# -------------------------------------------------------------------------
# arXiv scout — the first scout; reference shape for the rest.
# -------------------------------------------------------------------------


async def scout_arxiv(
    topics: list[str],
    per_topic: int = 5,
    *,
    start: int = 0,
    sort: str = "submittedDate",
) -> list[SourceDescriptor]:
    """Scout arXiv for papers matching `topics`.

    Queries arXiv once per topic (up to `per_topic` results each), dedupes by arXiv
    id across topics (first topic to surface a paper wins, and its topic is recorded
    in `why`), and returns `SourceDescriptor`s with kind='paper', source_kind='arxiv',
    canonical_key=<arxiv_id>. `start` pages deeper into each topic's results.

    `sort` ∈ {"submittedDate" (default, newest-first — the standing sweep),
    "relevance" (best-match-first — TARGETED searches on niche topics; see search_arxiv)}.

    PURE: returns descriptors only. It does NOT emit `source.discovered` events
    and does NOT touch the DB — the Librarian handler wires emission + dedupe
    against already-ingested docs. That keeps this scout testable and decoupled.
    """
    seen: dict[str, SourceDescriptor] = {}
    for i, topic in enumerate(topics):
        if i:
            await asyncio.sleep(_SCOUT_TOPIC_DELAY)  # space arXiv calls across topics
        query = topic if ":" in topic else f"all:{topic}"
        try:
            results = await search_arxiv(query, max_results=per_topic, start=start, sort=sort)
        except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
            log.warning("scout_arxiv: topic %r failed: %s", topic, e)
            continue

        for r in results:
            if r.arxiv_id in seen:
                continue
            seen[r.arxiv_id] = SourceDescriptor(
                kind="paper",
                source_kind="arxiv",
                canonical_key=r.arxiv_id,
                url=r.pdf_url,
                arxiv_id=r.arxiv_id,
                doi=None,
                title=r.title or None,
                why=f"arxiv topic: {topic}",
            )

    return list(seen.values())


# -------------------------------------------------------------------------
# Web scout — SearXNG (kind='web'). Self-contained (no agents.* import) so the
# library layer stays independent. Best-effort: empty list on any failure.
# -------------------------------------------------------------------------


async def scout_web(topics: list[str], per_topic: int = 5, *, start: int = 0) -> list[SourceDescriptor]:
    """Scout the open web (SearXNG) for pages on `topics`. canonical_key is the
    URL. `start` pages deeper (SearXNG `pageno`). PURE: returns descriptors only."""
    seen: dict[str, SourceDescriptor] = {}
    pageno = start // max(per_topic, 1) + 1
    async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _SCOUT_UA}) as client:
        for topic in topics:
            try:
                resp = await client.get(
                    f"{_SEARXNG_URL}/search",
                    params={"q": topic, "format": "json", "categories": "general", "pageno": pageno},
                )
                if resp.status_code != 200:
                    # A non-200 means SearXNG is down, rate-limiting, or — as we
                    # once hit — SEARXNG_URL points at the wrong service entirely
                    # (a 404 from whatever else grabbed the port). Never silent:
                    # a misconfigured search backend should be loud, not yield [].
                    log.warning(
                        "scout_web: SearXNG at %s returned HTTP %d for %r — "
                        "web discovery degraded (check SEARXNG_URL / container)",
                        _SEARXNG_URL,
                        resp.status_code,
                        topic,
                    )
                    continue
                results = resp.json().get("results", [])
            except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
                log.warning("scout_web: topic %r failed (%s): %s", topic, _SEARXNG_URL, e)
                continue
            for r in results[:per_topic]:
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen[url] = SourceDescriptor(
                    kind="web",
                    source_kind="web",
                    canonical_key=url,
                    url=url,
                    title=(r.get("title") or None),
                    why=f"web topic: {topic}",
                )
    return list(seen.values())


# -------------------------------------------------------------------------
# GitHub scout — repo search (kind='code'). Uses GITHUB_TOKEN if set.
# -------------------------------------------------------------------------


async def scout_github(topics: list[str], per_topic: int = 5, *, start: int = 0) -> list[SourceDescriptor]:
    """Scout GitHub for repos matching `topics` (sorted by stars). canonical_key
    is 'owner/repo'. `start` pages deeper (GitHub `page`). PURE: returns
    descriptors only. Best-effort."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _SCOUT_UA}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    seen: dict[str, SourceDescriptor] = {}
    page = start // max(per_topic, 1) + 1
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
        for i, topic in enumerate(topics):
            if i:
                await asyncio.sleep(_SCOUT_TOPIC_DELAY)  # GitHub search is rate-limited
            try:
                resp = await client.get(
                    f"{_GITHUB_API}/search/repositories",
                    params={"q": topic, "sort": "stars", "per_page": per_topic, "page": page},
                )
                items = resp.json().get("items", []) if resp.status_code == 200 else []
            except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
                log.warning("scout_github: topic %r failed: %s", topic, e)
                continue
            for it in items:
                full = it.get("full_name")
                if not full or full in seen:
                    continue
                seen[full] = SourceDescriptor(
                    kind="code",
                    source_kind="github",
                    canonical_key=full,
                    url=it.get("html_url"),
                    title=full,
                    why=f"github topic: {topic}",
                )
    return list(seen.values())


# -------------------------------------------------------------------------
# Dataset scout — HuggingFace hub (kind='dataset'). The lab's read on the DATA
# landscape: which datasets exist for a topic and which are trending (ranked by
# downloads). canonical_key is the HF dataset id ('owner/name'); the descriptor
# url is the dataset page, which Mimir ingests via the normal web-fetch path
# (the dataset card becomes the document text). PURE: returns descriptors only.
# -------------------------------------------------------------------------


async def scout_dataset(topics: list[str], per_topic: int = 5, *, start: int = 0) -> list[SourceDescriptor]:
    """Scout the HuggingFace dataset hub for datasets matching `topics`, ranked
    by downloads (popularity = the field's current data landscape). canonical_key
    is the HF dataset id; url is the dataset page. `start` is accepted for a
    uniform scout interface but the HF list API has no stable offset, so this
    scout stays refresh-oriented (top-by-downloads). PURE: returns descriptors
    only. Best-effort: empty list on any failure, one bad topic never sinks it."""
    _ = start  # HF list API doesn't paginate by offset; cursor stays at refresh
    seen: dict[str, SourceDescriptor] = {}
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _SCOUT_UA}) as client:
        for topic in topics:
            try:
                resp = await client.get(
                    f"{_HF_API}/api/datasets",
                    params={"search": topic, "sort": "downloads", "direction": -1, "limit": per_topic},
                )
                items = resp.json() if resp.status_code == 200 else []
            except Exception as e:  # noqa: BLE001 — one bad topic must not sink the sweep
                log.warning("scout_dataset: topic %r failed: %s", topic, e)
                continue
            for it in items[:per_topic]:
                ds_id = it.get("id")
                if not ds_id or ds_id in seen:
                    continue
                downloads = it.get("downloads")
                seen[ds_id] = SourceDescriptor(
                    kind="dataset",
                    source_kind="dataset",
                    canonical_key=ds_id,
                    url=f"{_HF_API}/datasets/{ds_id}",
                    title=ds_id,
                    why=f"dataset topic: {topic} (HF downloads={downloads})",
                )
    return list(seen.values())


# -------------------------------------------------------------------------
# OpenML scout — benchmark datasets (kind='dataset', source_kind='openml'). The
# classical-ML dataset landscape: tabular/benchmark datasets used across many
# experiments. OpenML's free-text search is weak, so we match the topic against
# dataset NAMES (data_name filter) and fall back to active datasets; the ingest
# resolver enriches each with its description + qualities (instances/features/
# classes). canonical_key='openml:<did>'. PURE: returns descriptors only.
# -------------------------------------------------------------------------


def _openml_topic_tokens(topic: str) -> list[str]:
    """Lowercased alpha tokens (len>=4) used to substring-match dataset names."""
    return [w.lower() for w in re.findall(r"[A-Za-z]{4,}", topic)]


async def _openml_active_pool(client: httpx.AsyncClient, limit: int, start: int) -> list[dict]:
    """One page of active datasets (did-ordered) for the substring fallback. Logs on a
    real failure (vs the silent [] the old scout returned, hiding outages — finding #2)."""
    off = f"/offset/{start}" if start else ""
    try:
        resp = await client.get(f"{_OPENML_API}/data/list/status/active/limit/{limit}{off}")
        if resp.status_code != 200:
            log.warning("scout_openml: active-list HTTP %d at %s — OpenML degraded", resp.status_code, _OPENML_API)
            return []
        return resp.json().get("data", {}).get("dataset", []) or []
    except Exception as e:  # noqa: BLE001
        log.warning("scout_openml: active-list fetch failed (%s): %s", _OPENML_API, e)
        return []


async def _openml_exact(client: httpx.AsyncClient, name: str, per_topic: int) -> list[dict]:
    """Exact data_name match (precise when a topic IS a dataset name). A 412 'No
    results' is the EXPECTED miss signal here, not an error — return [] quietly."""
    try:
        resp = await client.get(f"{_OPENML_API}/data/list/data_name/{name}/status/active/limit/{per_topic}")
        if resp.status_code != 200:
            return []
        return resp.json().get("data", {}).get("dataset", []) or []
    except Exception:  # noqa: BLE001
        return []


async def scout_openml(topics: list[str], per_topic: int = 5, *, start: int = 0) -> list[SourceDescriptor]:
    """Scout OpenML for benchmark datasets matching `topics`.

    OpenML's `data_name` filter is EXACT (a miss returns HTTP 412), so a subfield
    topic matched nothing. We now combine: (1) an exact `data_name` lookup — precise
    when a topic literally names a dataset (mnist_784, iris, credit-g) — with (2) a
    substring match of the topic's tokens against a once-fetched page of active
    datasets. `start` pages the active pool deeper. Best-effort; one bad topic never
    sinks the sweep. PURE: returns descriptors only."""
    seen: dict[str, SourceDescriptor] = {}
    async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _SCOUT_UA}) as client:
        pool = await _openml_active_pool(client, _OPENML_ACTIVE_POOL, start)
        for topic in topics:
            tokens = _openml_topic_tokens(topic)
            kw = next((w for w in topic.split() if w.isalpha() and len(w) > 3), "")
            exact = await _openml_exact(client, kw, per_topic) if kw else []
            substr = [d for d in pool if any(t in (d.get("name") or "").lower() for t in tokens)] if tokens else []
            added = 0
            for d in exact + substr:  # exact first, then substring; dedupe by did
                did = d.get("did")
                if did is None:
                    continue
                key = f"openml:{did}"
                if key in seen:
                    continue
                seen[key] = SourceDescriptor(
                    kind="dataset",
                    source_kind="openml",
                    canonical_key=key,
                    url=f"https://www.openml.org/d/{did}",
                    title=d.get("name"),
                    why=f"openml dataset (topic: {topic})",
                )
                added += 1
                if added >= per_topic:
                    break
    return list(seen.values())
