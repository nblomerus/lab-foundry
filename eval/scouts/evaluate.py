"""
Scout (discovery-layer) evaluation — the third substrate pillar.

The five scouts (arXiv / Web / GitHub / OpenML / HF-dataset) are the front door:
they discover candidates that Mimir then trust-gates and the corpus then serves.
Garbage in here is filtered by Mimir's gate, but irrelevant/empty/malformed
discovery wastes the whole pipeline and starves Ariadne of fresh material.

Each scout is a PURE async fn returning list[SourceDescriptor]. This LIVE eval
(hits arXiv/SearXNG/GitHub/OpenML/HF) measures, per scout:

  * CONTRACT  — every descriptor well-formed: correct source_kind/kind, non-empty
                canonical_key + url. Deterministic given the output; a violation is
                a real bug. (FAIL)
  * DEDUP     — no duplicate canonical_key within a run. (FAIL)
  * LIVENESS  — a known-good canary topic returns results; 0 results => the source
                is unreachable/empty, reported SKIP (not FAIL) so an outage never
                looks like a scout bug.
  * RELEVANCE@k — coarse LEXICAL proxy: fraction of titles containing a topic
                token. A signal, not a gate (datasets/repos name things, so a low
                lexical score isn't necessarily low relevance). Reported, not failed.
  * ROBUSTNESS — empty topic list returns [] without a network call or a crash.

    python -m eval.scouts.evaluate

Cross-source dedup (same paper from arXiv AND web) is the Librarian handler's job
(dedupe by (source_kind, canonical_key)), NOT a scout's — so it is out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from library.ingest.scouts import (
    SourceDescriptor,
    scout_arxiv,
    scout_dataset,
    scout_github,
    scout_openml,
    scout_web,
)

log = logging.getLogger("eval.scouts")

# Per-scout canary topic (must return results from a healthy source) + the expected
# contract + the topic tokens used for the coarse relevance proxy. Topics are chosen
# to suit each source (OpenML/HF match NAMES, not abstract subfields).
_SEARXNG = os.environ.get("SEARXNG_URL", "http://localhost:8081")
SCOUTS = [
    {
        "name": "arxiv",
        "fn": scout_arxiv,
        "topic": "transformer language model",
        "source_kind": "arxiv",
        "kind": "paper",
        "terms": ["transformer", "language", "model"],
        "reach": "http://export.arxiv.org/api/query?search_query=all:machine+learning&max_results=1",
    },
    {
        "name": "web",
        "fn": scout_web,
        "topic": "retrieval augmented generation",
        "source_kind": "web",
        "kind": "web",
        "terms": ["retrieval", "augmented", "generation", "rag"],
        "reach": f"{_SEARXNG}/search?q=test&format=json",
    },
    {
        "name": "github",
        "fn": scout_github,
        "topic": "transformer",
        "source_kind": "github",
        "kind": "code",
        "terms": ["transformer"],
        "reach": "https://api.github.com/search/repositories?q=test&per_page=1",
    },
    {
        "name": "dataset",
        "fn": scout_dataset,
        "topic": "sentiment",
        "source_kind": "dataset",
        "kind": "dataset",
        "terms": ["sentiment"],
        "reach": "https://huggingface.co/api/datasets?limit=1",
    },
    {
        "name": "openml",
        "fn": scout_openml,
        "topic": "mnist",
        "source_kind": "openml",
        "kind": "dataset",
        "terms": ["mnist"],
        "reach": "https://www.openml.org/api/v1/json/data/list/status/active/limit/1",
    },
]


async def _reachable(url: str) -> bool:
    """True if the source itself answers 200 — lets us tell 'source down' (SKIP)
    from 'source up but the scout found nothing' (EMPTY = a real scout weakness)."""
    headers = {"User-Agent": "labfoundry-scout-eval"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as c:
            return (await c.get(url)).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _contract_issues(d: SourceDescriptor, spec: dict) -> list[str]:
    issues = []
    if d.source_kind != spec["source_kind"]:
        issues.append(f"source_kind={d.source_kind!r}!={spec['source_kind']!r}")
    if d.kind != spec["kind"]:
        issues.append(f"kind={d.kind!r}!={spec['kind']!r}")
    if not (d.canonical_key and d.canonical_key.strip()):
        issues.append("empty canonical_key")
    if not (d.url and d.url.strip()):
        issues.append("empty url (ingest needs a fetch target)")
    return issues


def _relevance(descriptors: list[SourceDescriptor], terms: list[str]) -> float:
    if not descriptors:
        return 0.0
    pat = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    hits = sum(1 for d in descriptors if d.title and pat.search(d.title))
    return hits / len(descriptors)


async def _eval_scout(spec: dict) -> dict:
    name = spec["name"]
    # robustness: empty topic list must short-circuit to [] (no network, no crash)
    try:
        empty = await spec["fn"]([])
        robust = empty == []
    except Exception as e:  # noqa: BLE001
        robust = False
        log.warning("scout %s raised on empty topics: %s", name, e)

    try:
        descriptors = await spec["fn"]([spec["topic"]], per_topic=5)
    except Exception as e:  # noqa: BLE001 — scouts are best-effort; a raise is itself a bug
        return {"name": name, "status": "FAIL", "error": f"raised: {e}", "robust": robust}

    count = len(descriptors)
    if count == 0:
        # Distinguish a source outage (SKIP) from a scout that returns nothing
        # against a HEALTHY source (EMPTY = a real weakness/bug, e.g. OpenML's
        # exact-name 412 that the scout swallows into []).
        up = await _reachable(spec["reach"])
        if up:
            return {
                "name": name,
                "status": "EMPTY",
                "robust": robust,
                "note": f"source is UP (200) but the scout returned 0 for canary "
                f"{spec['topic']!r} — scout ineffective, not an outage",
            }
        return {
            "name": name,
            "status": "SKIP",
            "robust": robust,
            "note": f"source unreachable for canary {spec['topic']!r} — outage, not a scout bug",
        }

    keys = [d.canonical_key for d in descriptors]
    dups = len(keys) - len(set(keys))
    issues = []
    for d in descriptors:
        issues.extend(f"{d.canonical_key}: {i}" for i in _contract_issues(d, spec))
    rel = _relevance(descriptors, spec["terms"])
    status = "FAIL" if (issues or dups or not robust) else "PASS"
    return {
        "name": name,
        "status": status,
        "count": count,
        "dups": dups,
        "contract_issues": issues,
        "relevance": rel,
        "robust": robust,
    }


def _render(results: list[dict]) -> int:
    print("\n" + "=" * 80)
    print("SCOUT EVAL — contract / dedup / liveness / relevance@k (live sources)")
    print("=" * 80)
    fails = 0
    for r in results:
        if r["status"] == "SKIP":
            print(f"  {r['name']:<9} SKIP   {r.get('note', '')}")
            continue
        if r["status"] == "EMPTY":
            fails += 1  # source up + scout empty is a real weakness, not an outage
            print(f"  {r['name']:<9} EMPTY  {r.get('note', '')}")
            continue
        if r.get("error"):
            fails += 1
            print(f"  {r['name']:<9} FAIL   {r['error']}")
            continue
        if r["status"] == "FAIL":
            fails += 1
        flags = []
        if r["dups"]:
            flags.append(f"DUPS={r['dups']}")
        if not r["robust"]:
            flags.append("EMPTY-TOPIC-NOT-[]")
        if r["contract_issues"]:
            flags.append(f"CONTRACT×{len(r['contract_issues'])}")
        print(
            f"  {r['name']:<9} {r['status']:<5}  n={r['count']:<3} "
            f"relevance@k={r['relevance']:.2f}  dedup={'ok' if not r['dups'] else 'BAD'}  "
            f"contract={'ok' if not r['contract_issues'] else 'BAD'}  {' '.join(flags)}"
        )
        for i in r["contract_issues"][:5]:
            print(f"               - {i}")
    print("=" * 80)
    print("  PASS = well-formed + dedup-clean + robust. SKIP = source returned nothing")
    print("  (outage/empty, not a bug). relevance@k is a COARSE lexical proxy — a low")
    print("  value for dataset/repo scouts can be naming, not irrelevance.")
    print()
    return 1 if fails else 0


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    results = []
    for spec in SCOUTS:
        results.append(await _eval_scout(spec))
    raise SystemExit(_render(results))


if __name__ == "__main__":
    asyncio.run(main())
