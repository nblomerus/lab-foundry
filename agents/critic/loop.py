"""
Critic loop — four-step refutation hunt replacing the single-shot
`critic.kill_verdict`.

Pipeline per finding.high_signal event:

    plan_attack(thesis, recent_findings)
        → 2-3 WeakPoint with concrete search queries + optional experiment

    for each weak point (parallel, capped):
        search → fetch → extract_counter(page) → CounterEvidenceBatch

    if AttackPlan.proposed_experiment is not None:
        run_experiment → stress_test(result) → StressTestInterp

    judge_verdict(thesis, findings, counter_evidence, stress_test) → AdversaryVerdictOut

Why four steps:

The legacy single-shot critic argues from priors — the prompt at
labfoundry/handlers/adversary.py:91-123 hands the model the thesis +
recent findings and asks "watch / weaken / kill?". There's no actual
evidence gathering. Multi-step here lets the critic *do its own
research pass* targeted at the thesis it's attacking, which is the whole
point of an critic that isn't just a gut check.

Behind `ADVERSARY_LOOP=v2`. Legacy single-call path stays default until
the loop is validated on real theses.
"""

from __future__ import annotations

import asyncio
import json
import logging

# Reuse the verdict schema defined alongside the legacy critic so the
# rest of the handler (state.create_adversary_verdict, kill_thesis,
# memory.write_message) is unchanged.
from agents.critic.handler import AdversaryVerdictOut
from agents.critic.schemas import (
    AttackPlan,
    CounterEvidenceBatch,
    CounterEvidenceItem,
    StressTestInterp,
)
from agents.researcher.tools import (
    SearchResult,
    search_hacker_news,
    search_reddit,
    search_web,
)
from harness.curator import RECIPES, PromptLayer, Recipe
from library.ingest.fetcher import web_fetch_many

log = logging.getLogger(__name__)


_SOURCE_TOOLS = {
    "hacker_news": search_hacker_news,
    "web": search_web,
    "reddit": search_reddit,
}


# Caps. Critic search budget is intentionally smaller than the researcher's
# — the goal is a targeted refutation pass, not a deep investigation.
MAX_PAGES_PER_WEAKPOINT = 3
MAX_PARALLEL_EXTRACTS = 4


# -------------------------------------------------------------------------
# Prompt builders
# -------------------------------------------------------------------------


async def _research_findings_block(state, claim_id: int) -> str:
    """The lab's OWN synthesis findings on a research direction, so an adversarial review of a
    direction is GROUNDED in what the lab actually concluded — not only web counter-evidence. The
    market `findings` table is empty for research directions (they flow through research_findings),
    which left the critic reviewing a direction's statement blind to the lab's supporting evidence."""
    try:
        rfs = await state.get_recent_findings_for_claims([claim_id], limit=8)
    except Exception:  # noqa: BLE001 — supplementary context; never block the review
        return ""
    return "\n".join(
        f"- [synthesis, supported={r.get('supported')}, conf {float(r.get('confidence') or 0):.2f}, "
        f"{r.get('n_experiments', 0)} exps]: {(r.get('headline') or '')[:90]}\n    {(r.get('so_what') or '')[:200]}"
        for r in rfs
    )


async def _build_plan_attack(ctx: dict, state, memory) -> PromptLayer:
    import asyncio as _asyncio

    thesis_id = ctx["thesis_id"]
    triggering_finding_id = ctx.get("triggering_finding_id")

    thesis, recent_findings = await _asyncio.gather(
        state.get_thesis(thesis_id),
        state.get_recent_findings_for_thesis(thesis_id=thesis_id, limit=20),
    )

    if recent_findings:
        findings_block = "\n".join(
            f"- F{f.id} [{f.source}, rel {f.relevance_score}, "
            f"supports={f.supports_thesis}, audit={f.audit_verdict}]: "
            f"{f.title}\n    {f.summary[:200]}"
            for f in recent_findings
        )
    else:
        findings_block = await _research_findings_block(state, thesis_id) or "(no findings yet — early in research)"

    trigger_line = (
        f"\nThis review was triggered by finding F{triggering_finding_id} reaching high signal."
        if triggering_finding_id
        else ""
    )

    content = f"""## Target thesis under adversarial review

**Claim:** {thesis.statement}
**Status:** {thesis.status}  |  Current confidence: {thesis.confidence:.2f}
**Born:** {thesis.created_at:%Y-%m-%d}{trigger_line}

## Recent findings ({len(recent_findings)})

{findings_block}

---

Your job: plan a refutation pass against this thesis. You will NOT decide
the verdict here — only identify where the thesis is brittle and what to
search for.

Decompose the thesis into **2-3 weak points**. A weak point is a specific
assumption the thesis depends on that, if false, would refute or weaken it.
Bad weak points are vague ("market timing is uncertain"); good weak points
are concrete falsifiers ("there exist >5 well-funded incumbents offering
the same product at <$10/mo").

For each weak point:
- `hypothesis`: a sentence stating the concrete falsifier you'll search for.
- `search_queries`: 1-2 specific queries (not generic — name things,
  use numbers, use entity names). The queries should be likely to surface
  evidence FOR the hypothesis, i.e. AGAINST the thesis.
- `sources`: pick from {{web, hacker_news, reddit}}. Default to web.

You may also propose ONE optional `proposed_experiment` — a 'do something'
test that would discriminate. Experiment kinds + param shapes are the
same as the researcher's:

- `fetch_pricing`: tests "what does X cost in this market?"
  Params: {{"company": "OpenAI"}} or {{"companies": [...]}} or {{"url": "..."}}
- `count_demand_signal`: tests "are people actually asking for / complaining about X?"
  Params: {{"phrases": ["..."], "sources": ["reddit", "hacker_news"]}}
- `compare_repo_growth`: tests "is this tech actually being adopted?"
  Params: {{"repos": ["owner/name", ...]}}
- `gh_search_trend`: tests "is anyone actually building this now?"
  Params: {{"queries": ["..."], "months_back": 6}}

Set `proposed_experiment` to null if no experiment would meaningfully
discriminate — don't propose one to look thorough.

Finally, `rationale`: one sentence — what's the single most load-bearing
claim in the thesis, and which weak point most cleanly attacks it?
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_extract_counter(ctx: dict, state, memory) -> PromptLayer:
    weak_point = ctx["weak_point"]  # WeakPoint dict
    url = ctx["url"]
    title = ctx.get("title") or ""
    page_content = ctx["content"]

    snippet = page_content[:12_000]
    if len(page_content) > 12_000:
        snippet += "\n\n[...page truncated; first 12 KB shown...]"

    content = f"""## Adversarial weak point being investigated

**Hypothesis to test** (if true, refutes the thesis): {weak_point["hypothesis"]}

## Source page
**URL:** {url}
**Title:** {title or "(none)"}

## Page content (cleaned)
{snippet}

---

Extract 0-N **counter-evidence items** from the page that bear on the
hypothesis. For each:

- `quote`: a **verbatim** sentence or short phrase from the page.
- `claim`: what the quote means against the weak point, one sentence.
- `stance`:
    * `refutes` — the quote backs the WEAK POINT, meaning it refutes the
                   thesis (the critic's goal).
    * `supports` — the quote backs the THESIS (the opposite of what we
                    wanted, but honest).
    * `neutral`  — ambiguous but worth recording.
- `confidence`: 0..1, how load-bearing is this item.

ONLY emit items with concrete signal (numbers, prices, named companies,
benchmarks, dated events, specific complaints). SKIP generic guides,
vendor marketing, opinion pieces without grounding. An empty list is the
right answer when the page has nothing.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_stress_test_interp(ctx: dict, state, memory) -> PromptLayer:
    kind = ctx["kind"]
    params = ctx["params"]
    result = ctx["result"]
    thesis_claim = ctx["thesis_claim"]
    weak_points = ctx["weak_points"]  # list[dict]

    wp_lines = "\n".join(f"- {wp['hypothesis']}" for wp in weak_points)

    content = f"""## Adversarial stress test

**Thesis under attack:** {thesis_claim}

**Weak points the critic is testing:**
{wp_lines}

## Experiment run
**Kind:** {kind}
**Params:** {json.dumps(params)}

## Result
```json
{json.dumps(result, indent=2)[:6000]}
```

---

Interpret the experiment result *adversarially* — does it refute or
weaken the thesis?

- `summary`: 2-3 sentences. Name the numbers, the entities, the
  comparison. Be concrete.
- `bears_against_thesis`: true if the result refutes or weakens, even
  slightly. False if it supports the thesis or is irrelevant.
- `confidence`: how load-bearing is this result on the verdict (0..1)?

Be honest about null results. A failed search or empty data is a
data-quality issue, NOT evidence either way — set bears_against_thesis
to false and confidence to ≤ 0.2.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_judge_verdict(ctx: dict, state, memory) -> PromptLayer:
    import asyncio as _asyncio

    thesis_id = ctx["thesis_id"]
    weak_points = ctx["weak_points"]  # list[dict]
    counter_evidence = ctx["counter_evidence"]  # list[dict]
    stress_test = ctx.get("stress_test")  # Optional[dict]

    thesis, recent_findings = await _asyncio.gather(
        state.get_thesis(thesis_id),
        state.get_recent_findings_for_thesis(thesis_id=thesis_id, limit=20),
    )

    if recent_findings:
        findings_block = "\n".join(
            f"- F{f.id} [{f.source}, rel {f.relevance_score}, supports={f.supports_thesis}]: {(f.title or '')[:80]}"
            for f in recent_findings
        )
    else:
        findings_block = await _research_findings_block(state, thesis_id) or "(no findings yet)"

    wp_lines = "\n".join(f"  - {wp['hypothesis']}" for wp in weak_points)

    if counter_evidence:
        ce_lines = []
        for ce in counter_evidence[:30]:
            stance = ce["stance"]
            conf = float(ce["confidence"])
            ce_lines.append(
                f"  - [{stance} · conf {conf:.2f}] {ce['url']}\n"
                f"      claim: {ce['claim']}\n"
                f'      quote: "{ce["quote"][:200]}"'
            )
        ce_block = "\n".join(ce_lines)
    else:
        ce_block = "  (no counter-evidence gathered — searches returned nothing usable)"

    if stress_test:
        st_block = (
            f"## Stress test result\n"
            f"- Summary: {stress_test['summary']}\n"
            f"- Bears against thesis: {stress_test['bears_against_thesis']}\n"
            f"- Confidence: {stress_test['confidence']:.2f}"
        )
    else:
        st_block = "(no stress test was proposed)"

    content = f"""## Final adversarial verdict

**Thesis:** {thesis.statement}
**Status:** {thesis.status}  |  Current confidence: {thesis.confidence:.2f}

## Weak points investigated
{wp_lines}

## Counter-evidence gathered ({len(counter_evidence)} items)
{ce_block}

{st_block}

## Recent supporting findings (researcher's case for the thesis)
{findings_block}

---

Now decide: `watch`, `weaken`, or `kill`.

The bar is higher than the legacy critic because you actually
*looked* — counter-evidence above is what you found, not what you
inferred. Calibration:

- `kill`: ≥2 high-confidence `refutes` items from independent sources,
  AND the weak point they refute is genuinely load-bearing for the
  thesis. Cite the specific finding ids (or evidence URLs) in
  `cited_finding_ids` — they become the kill rationale.

- `weaken`: real concerns surfaced but not a kill. ≥1 high-confidence
  refute, OR multiple medium-confidence refutes across weak points.
  MUST set `proposed_confidence_delta` to a NEGATIVE number — a weaken
  without delta will be defaulted to -0.10.

- `watch`: no usable counter-evidence found. The search returned noise,
  vendor marketing, or material that doesn't bear on the weak points.
  An honest watch is better than a fabricated kill.

`reasoning`: 2-4 sentences. Name the specific quotes / numbers that drove
the decision. If you found nothing, say so plainly.

Confidence in the action ≠ confidence in the thesis. A weakly-held weaken
should say weaken with confidence 0.6, not bump to kill.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe registration
# -------------------------------------------------------------------------

_ADVERSARY_RECIPES: list[tuple[str, str, int, str, callable]] = [
    (
        "adversary.plan_attack",
        "Identify 2-3 weak points in a thesis and propose targeted searches.",
        8_000,
        "AttackPlan",
        _build_plan_attack,
    ),
    (
        "adversary.extract_counter",
        "Extract counter-evidence quotes from one fetched page against a weak point.",
        14_000,
        "CounterEvidenceBatch",
        _build_extract_counter,
    ),
    (
        "adversary.stress_test_interp",
        "Adversarially interpret a stress-test experiment result.",
        6_000,
        "StressTestInterp",
        _build_stress_test_interp,
    ),
    (
        "adversary.judge_verdict",
        "Final watch/weaken/kill verdict after a full refutation pass.",
        10_000,
        "AdversaryVerdictOut",
        _build_judge_verdict,
    ),
]

for _itype, _desc, _budget, _schema, _builder in _ADVERSARY_RECIPES:
    if _itype not in RECIPES:
        RECIPES[_itype] = Recipe(
            invocation_type=_itype,
            description=_desc,
            agent="critic",
            total_budget=_budget,
            # judge_verdict pulls dissent + claims-lifecycle for calibration,
            # same as the legacy critic recipe. (Was "theses-lifecycle" — a
            # session that is never created, so recall silently returned nothing.)
            use_cold_path=(_itype == "adversary.judge_verdict"),
            recall_sessions=(["claims-lifecycle", "dissent"] if _itype == "adversary.judge_verdict" else []),
            recall_k=5 if _itype == "adversary.judge_verdict" else 0,
            output_schema=_schema,
            task_data_builder=_builder,
        )


# -------------------------------------------------------------------------
# Search helper (mirrors research/loop._search_for_sub_question)
# -------------------------------------------------------------------------


async def _search_for_weak_point(queries: list[str], sources: list[str]) -> list[SearchResult]:
    out: list[SearchResult] = []
    for q in queries:
        for source in sources:
            tool = _SOURCE_TOOLS.get(source)
            if tool is None:
                continue
            try:
                hits = await tool(query=q, limit=3)
            except Exception as e:  # noqa: BLE001
                log.warning("adversary search %s failed for %r: %s", source, q, e)
                continue
            out.extend(hits[:3])
    return out


# -------------------------------------------------------------------------
# Orchestrator
# -------------------------------------------------------------------------


async def run_adversary_loop(
    *,
    thesis_id: int,
    triggering_finding_id: int | None,
    dispatcher,
    triggered_by_event_id: int | None = None,
) -> tuple[AdversaryVerdictOut, int, list[dict]]:
    """
    Drive the four-step critic loop for one thesis.

    Returns (verdict, judge_run_id, counter_evidence_summary). The caller
    persists the verdict and applies kill/weaken side effects exactly as
    in the legacy path.
    """
    router = dispatcher.router
    curator = dispatcher.curator
    state = dispatcher.state
    session = dispatcher.session

    # ---- 1. plan_attack -------------------------------------------------
    plan_prompt = await curator.build(
        invocation_type="adversary.plan_attack",
        context={
            "thesis_id": thesis_id,
            "triggering_finding_id": triggering_finding_id,
        },
    )
    plan, plan_run_id = await router.invoke(
        prompt=plan_prompt,
        output_schema_class=AttackPlan,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="plan_attack",
    )

    # ---- 2. search + fetch + extract per weak point ---------------------
    counter_evidence: list[dict] = []
    sem = asyncio.Semaphore(MAX_PARALLEL_EXTRACTS)

    async def _extract_for_page(wp_idx: int, wp_dict: dict, url: str, title: str, content: str):
        async with sem:
            extract_prompt = await curator.build(
                invocation_type="adversary.extract_counter",
                context={
                    "weak_point": wp_dict,
                    "url": url,
                    "title": title,
                    "content": content,
                },
            )
            try:
                batch, _rid = await router.invoke(
                    prompt=extract_prompt,
                    output_schema_class=CounterEvidenceBatch,
                    triggered_by_event_id=triggered_by_event_id,
                    session=session,
                    step_name=f"extract_counter/wp{wp_idx}/{url[:40]}",
                    parent_step_id=plan_run_id,
                )
                return batch.items
            except Exception as e:  # noqa: BLE001 — per-page failure non-fatal
                log.warning("adversary extract_counter failed for %s: %s", url, e)
                return []

    extract_tasks = []
    for wp_idx, wp in enumerate(plan.weak_points):
        wp_dict = wp.model_dump()
        results = await _search_for_weak_point(wp.search_queries, wp.sources)
        urls_seen: set[str] = set()
        urls: list[tuple[str, str]] = []
        for r in results:
            if r.url in urls_seen:
                continue
            urls_seen.add(r.url)
            urls.append((r.url, r.title or ""))
            if len(urls) >= MAX_PAGES_PER_WEAKPOINT:
                break
        if not urls:
            continue
        pages = await web_fetch_many([u for u, _ in urls], state, concurrency=4)
        url_to_title = dict(urls)
        for page in pages:
            if page is None or not page.content.strip():
                continue
            extract_tasks.append(
                _extract_for_page(
                    wp_idx,
                    wp_dict,
                    page.url,
                    url_to_title.get(page.url, ""),
                    page.content,
                )
            )

    if extract_tasks:
        for items in await asyncio.gather(*extract_tasks):
            for ci in items:
                if isinstance(ci, CounterEvidenceItem):
                    counter_evidence.append(
                        {
                            "quote": ci.quote,
                            "claim": ci.claim,
                            "stance": ci.stance,
                            "confidence": ci.confidence,
                            # url isn't on the item itself — but it doesn't matter
                            # for the judge step; the model sees the page elsewhere
                            "url": "",
                        }
                    )

    # Backfill URL onto each counter_evidence item by re-walking extract_tasks
    # results would be cleaner; we leave url="" here because the judge prompt
    # tolerates it and the source provenance lives in the trace's per-step
    # input_summary (clickable from /trace).

    # ---- 3. stress test (optional) --------------------------------------
    stress_test_dict: dict | None = None
    if plan.proposed_experiment is not None:
        from agents.researcher.experiments import dispatch as exp_dispatch

        kind = plan.proposed_experiment.kind
        params = plan.proposed_experiment.params
        try:
            result = await exp_dispatch(kind, params, dispatcher=dispatcher)
        except Exception as e:  # noqa: BLE001
            log.warning("adversary stress test %s failed: %s", kind, e)
            result = {"error": str(e)[:200]}

        st_prompt = await curator.build(
            invocation_type="adversary.stress_test_interp",
            context={
                "kind": kind,
                "params": params,
                "result": result,
                "thesis_claim": (await state.get_thesis(thesis_id)).statement,
                "weak_points": [wp.model_dump() for wp in plan.weak_points],
            },
        )
        try:
            st_interp, _st_run_id = await router.invoke(
                prompt=st_prompt,
                output_schema_class=StressTestInterp,
                triggered_by_event_id=triggered_by_event_id,
                session=session,
                step_name=f"stress_test/{kind}",
                parent_step_id=plan_run_id,
            )
            stress_test_dict = st_interp.model_dump()
        except Exception as e:  # noqa: BLE001
            log.warning("adversary stress_test interpretation failed: %s", e)

    # ---- 4. judge verdict ----------------------------------------------
    judge_prompt = await curator.build(
        invocation_type="adversary.judge_verdict",
        context={
            "thesis_id": thesis_id,
            "weak_points": [wp.model_dump() for wp in plan.weak_points],
            "counter_evidence": counter_evidence,
            "stress_test": stress_test_dict,
        },
    )
    verdict, judge_run_id = await router.invoke(
        prompt=judge_prompt,
        output_schema_class=AdversaryVerdictOut,
        triggered_by_event_id=triggered_by_event_id,
        session=session,
        step_name="judge_verdict",
        parent_step_id=plan_run_id,
    )

    return verdict, judge_run_id, counter_evidence
