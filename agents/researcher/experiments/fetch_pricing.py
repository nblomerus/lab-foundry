"""
Experiment: fetch_pricing.

Given a URL (or a company / product name to search for), fetch the pricing
page through the cache, then ask the model to extract a structured tier list.

Params (one of these must be present):
    url     : str          — direct link to the pricing page
    company : str          — search "{company} pricing" and pick the top result
    product : str          — alias for `company` (some plans propose this)

Result:
    {
      url: str,                       # the page actually fetched
      from_cache: bool,
      tiers: [
        {name, price_usd, period, currency, features: [str], notes}
      ],
      raw_text_sample: str,           # first 1 KB of cleaned page (debug aid)
      extraction_quality: "good" | "partial" | "empty"
    }
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from agents.researcher.experiments import REGISTRY
from agents.researcher.tools import search_web
from harness.curator import RECIPES, PromptLayer, Recipe
from library.ingest.fetcher import web_fetch

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Output schema
# -------------------------------------------------------------------------


class PricingTier(BaseModel):
    name: str
    price_usd: float | None = None
    period: Literal["month", "year", "one_time", "per_user_month", "per_user_year", "usage"] | None = None
    currency: str = "USD"
    features: list[str] = Field(default_factory=list, max_length=20)
    notes: str | None = None


class ParsedPricing(BaseModel):
    tiers: list[PricingTier] = Field(default_factory=list, max_length=10)
    extraction_quality: Literal["good", "partial", "empty"] = "empty"


# -------------------------------------------------------------------------
# Curator recipe for the parsing LLM call
# -------------------------------------------------------------------------


async def _build_parse_pricing(ctx: dict, state, memory) -> PromptLayer:
    url = ctx["url"]
    page_content = ctx["content"]

    snippet = page_content[:10_000]
    if len(page_content) > 10_000:
        snippet += "\n\n[...page truncated; first 10 KB shown...]"

    content = f"""## Pricing page
**URL:** {url}

## Page content (cleaned)
{snippet}

---

Extract the pricing tiers as structured data. For each tier:
- `name`: e.g. "Starter", "Pro", "Enterprise" — verbatim from the page
- `price_usd`: numeric. Convert non-USD if obvious; leave null if "contact sales" / hidden
- `period`: month | year | one_time | per_user_month | per_user_year | usage
- `features`: 3-8 short bullet strings, verbatim or near-verbatim
- `notes`: anything important not captured by the other fields (e.g. "starts at", "billed annually")

`extraction_quality`:
- `good` if every tier has name + price + period + features
- `partial` if some fields are missing but the structure is recoverable
- `empty` if the page has no pricing or is a "contact sales" wall

Skip add-ons / per-feature pricing tables — focus on the main plan tiers.
Return an empty list with quality=empty if there's no actual pricing on the page.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


if "researcher.parse_pricing" not in RECIPES:
    RECIPES["researcher.parse_pricing"] = Recipe(
        invocation_type="researcher.parse_pricing",
        description="Extract structured pricing tiers from a fetched pricing page.",
        agent="researcher",
        total_budget=12_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ParsedPricing",
        task_data_builder=_build_parse_pricing,
    )


# -------------------------------------------------------------------------
# Runner
# -------------------------------------------------------------------------


def _coerce_targets(params: dict) -> list[dict]:
    """Normalize the many shapes the planner might propose into a list of
    target descriptors. Each descriptor is either {"url": str} or
    {"company": str}.

    Accepted input shapes:
      - {"url": "https://..."}
      - {"company": "OpenAI"} | {"product": "GPT-5"}
      - {"urls": ["https://...", "https://..."]}
      - {"companies": ["A", "B"]} | {"products": [...]}
    """
    targets: list[dict] = []
    if u := params.get("url"):
        targets.append({"url": u})
    if c := (params.get("company") or params.get("product")):
        targets.append({"company": c})
    for u in params.get("urls") or []:
        if isinstance(u, str):
            targets.append({"url": u})
    for c in params.get("companies") or params.get("products") or []:
        if isinstance(c, str):
            targets.append({"company": c})
    return targets


async def _run_one(target: dict, *, dispatcher) -> dict:
    state = dispatcher.state
    url = target.get("url")
    if not url:
        company = target["company"]
        results = await search_web(f"{company} pricing", limit=5)
        if not results:
            return {"target": target, "error": f"no search results for {company!r}"}
        url = results[0].url
        log.info("fetch_pricing: resolved %r -> %s", company, url)

    page = await web_fetch(url, state)
    if page is None:
        return {"target": target, "url": url, "error": f"failed to fetch {url}"}
    if not page.content.strip():
        return {
            "target": target,
            "url": url,
            "from_cache": page.from_cache,
            "tiers": [],
            "raw_text_sample": "",
            "extraction_quality": "empty",
            "note": "page extracted to empty content",
        }

    prompt = await dispatcher.curator.build(
        invocation_type="researcher.parse_pricing",
        context={"url": url, "content": page.content},
    )
    parsed, _ = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ParsedPricing,
    )
    return {
        "target": target,
        "url": url,
        "from_cache": page.from_cache,
        "tiers": [t.model_dump() for t in parsed.tiers],
        "raw_text_sample": page.content[:1000],
        "extraction_quality": parsed.extraction_quality,
    }


async def run(params: dict, *, dispatcher) -> dict:
    """
    Resolve URL → fetch → parse with the model. Accepts a flexible variety of
    param shapes the planner may emit (singular `url`/`company`/`product`, or
    plural `urls`/`companies`/`products`). Returns a result dict containing
    each target's parse outcome.
    """
    targets = _coerce_targets(params)
    if not targets:
        raise ValueError(
            "fetch_pricing requires one of `url`, `company`, `product`, "
            "or the plural list forms `urls`/`companies`/`products`. "
            f"Got params keys: {list(params.keys())}"
        )

    results = []
    for t in targets[:5]:  # cap to keep the per-experiment cost bounded
        try:
            results.append(await _run_one(t, dispatcher=dispatcher))
        except Exception as e:  # noqa: BLE001 — single-target failure is non-fatal
            log.warning("fetch_pricing single target %r failed: %s", t, e)
            results.append({"target": t, "error": str(e)[:300]})
    return {"targets": targets, "results": results, "count": len(results)}


REGISTRY["fetch_pricing"] = run
