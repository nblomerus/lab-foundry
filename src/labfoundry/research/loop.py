"""
Agentic researcher loop.

Replaces the old single-shot `researcher.execute_task` with a structured
investigation:

    plan_inquiry  →  for each sub-question (search → fetch → extract_evidence)
                  →  (Phase 3) for each experiment (run → interpret)
                  →  synthesize  →  (Phase 3) gap_check → iterate?

Each step is its own LLM invocation type so the Debug research-tree view can
dissect them independently. The orchestrator persists `research_inquiries` and
`evidence` along the way, and emits final `findings` in the legacy shape so
the evaluation / critic / PI paths see no schema change.

The four curator recipes for the new invocation types are registered at
module load (mirroring `labfoundry/handlers/task_completed.py:105-116`).
"""

from __future__ import annotations

import json
import logging

from labfoundry.harness.curator import (
    RECIPES,
    PromptLayer,
    Recipe,
)
from labfoundry.mcp_servers.labfoundry_research.tools import (
    SearchResult,
    search_hacker_news,
    search_reddit,
    search_web,
)
from labfoundry.research.fetcher import web_fetch_many
from labfoundry.research.schemas import (
    EvidenceBatch,
    ExperimentInterpretation,
    GapCheck,
    InquiryPlan,
    Synthesis,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Source dispatch
# -------------------------------------------------------------------------

_SOURCE_TOOLS = {
    "hacker_news": search_hacker_news,
    "web": search_web,
    "reddit": search_reddit,
}


async def _search_for_sub_question(
    sub_question: str,
    sources: list[str],
    k: int,
) -> list[SearchResult]:
    """Hit each requested source and merge the top results.

    Order: results from earlier sources come first. We don't dedupe by URL
    because the same URL across two sources is unlikely; if it happens, the
    fetch cache short-circuits the second fetch anyway.
    """
    results: list[SearchResult] = []
    for source in sources:
        tool = _SOURCE_TOOLS.get(source)
        if tool is None:
            continue
        try:
            hits = await tool(query=sub_question, limit=k)
        except Exception as e:  # noqa: BLE001 — source failure is non-fatal
            log.warning("source %s failed for %r: %s", source, sub_question, e)
            continue
        results.extend(hits[:k])
    return results


# -------------------------------------------------------------------------
# Curator recipes — one task_data builder per new invocation type
# -------------------------------------------------------------------------


async def _build_plan_inquiry(ctx: dict, state, memory) -> PromptLayer:
    task_id = ctx["task_id"]
    question = ctx["question"]
    iteration = ctx.get("iteration", 1)
    prior_evidence = ctx.get("prior_evidence") or []

    task, theses = await _gather(state.get_task(task_id), state.get_active_theses(limit=10))

    thesis_lines = "\n".join(f"- T{t.id}: {t.claim}" for t in theses) or "(no active theses — exploratory work)"

    if prior_evidence:
        prior_lines = "\n".join(
            f"- [{e['stance']}, conf {e['confidence']:.2f}] {e['claim']}" for e in prior_evidence[:20]
        )
        prior_block = f"\n## Evidence already gathered ({len(prior_evidence)} items)\n{prior_lines}\n"
    else:
        prior_block = ""

    content = f"""## Research task

**Task:** {task.description}
**Framing question (iteration {iteration}):** {question}
**Target thesis:** {f"T{task.thesis_id}" if task.thesis_id else "(exploratory)"}

## Active theses (sub-questions should plausibly inform one of these)
{thesis_lines}
{prior_block}
---

Decompose the framing question into 3-5 **sub-questions** whose evidence would
collectively answer it. For each sub-question, name a small set of **sources**
to query (web / reddit / hacker_news) and a one-sentence reason. Then propose
**0-2 experiments** that would test the most load-bearing claim by *doing*
something rather than reading — pricing scrapes, demand-signal counts, repo
growth comparisons.

Be sharp about what "would resolve this question" means — a sub-question
whose answer can't tilt your conclusion is wasted work. If iteration > 1,
focus on the **gaps** the previous iteration left, not what's already covered.

Experiment kinds available. Use the EXACT param shapes shown — the runner
rejects misshapen params.

- `fetch_pricing`: scrape a competitor pricing page, return structured tiers.
  Tests "what does X cost in this market?"
  Params (one of these forms — DO NOT invent other keys):
    {{"url": "https://example.com/pricing"}}
    {{"company": "OpenAI"}}                  ← single string
    {{"companies": ["OpenAI", "Brave", "Serper"]}}   ← list of strings
    {{"urls": ["https://...", "https://..."]}}

- `count_demand_signal`: hit search APIs and count matches. Tests "are people
  actually complaining about / asking for this?"
  Params:
    {{"phrases": ["search API too expensive", "replace SerpAPI"],
      "sources": ["reddit", "hacker_news"]}}
  `sources` MUST be `"reddit"` and/or `"hacker_news"` (not "hn", not "twitter").
  `phrases` is a list of 1-5 short strings.

- `compare_repo_growth`: fetch GitHub stars + issues for a set of repos.
  Tests "is this tech actually being adopted?"
  Params:
    {{"repos": ["meilisearch/meilisearch", "typesense/typesense"]}}
  Each repo is the "owner/name" form, 1-6 repos. **Only include repos you
  are highly confident actually exist on GitHub** — do NOT invent names like
  "mcp/mcp" or "ai/agent-framework". Prefer official org accounts
  (e.g. `anthropic/claude-code`, `modelcontextprotocol/servers`,
  `langchain-ai/langchain`). If you're not sure of an exact owner/name,
  use `gh_search_trend` instead — it discovers repos by topic.

- `gh_search_trend`: compare repo-creation counts on GitHub between the most
  recent N months and the prior N months for given search queries. Catches
  *adoption inflection* — fills the gap between forum-chatter demand and
  specific-named-repo growth. Tests "is anyone actually building this now?"
  Params:
    {{"queries": ["agent memory", "language:rust mcp"], "months_back": 6}}
  Queries may use GitHub search qualifiers (language:, topic:, etc.).

Return JSON conforming to InquiryPlan.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_extract_evidence(ctx: dict, state, memory) -> PromptLayer:
    sub_question = ctx["sub_question"]
    url = ctx["url"]
    title = ctx.get("title") or ""
    page_content = ctx["content"]

    # Cap page content to keep the call cheap and focused. The full page is
    # already persisted in `fetch_cache` for anyone who needs it.
    snippet = page_content[:12_000]
    if len(page_content) > 12_000:
        snippet += "\n\n[...page truncated; first 12 KB shown...]"

    content = f"""## Sub-question
{sub_question}

## Source page
**URL:** {url}
**Title:** {title or "(none)"}

## Page content (cleaned)
{snippet}

---

Extract 0-N **evidence items** from the page that bear on the sub-question.
For each:

- `quote`: a **verbatim** sentence or short phrase from the page above.
  No paraphrasing. If the page does not contain a quotable line that bears
  on the sub-question, emit nothing for it.
- `claim`: what the quote means, in your words. One sentence.
- `stance`: does the evidence support, refute, or sit neutral to the
  sub-question? Pick honestly. Neutral and refutes are both useful answers.
- `confidence`: 0..1, how load-bearing is this item. A vendor's own marketing
  page asserting their product is great = 0.2. A 3rd-party benchmark with
  numbers = 0.8. A vague trend statement = 0.3.

ONLY emit evidence with a concrete signal — a number, a price, a named
company, a dated event, a real user complaint, a benchmark, an API quirk.
SKIP generic explainers, "guide" / "best practices" content, vendor marketing
without specifics. Empty list is the right answer when the page has nothing.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_synthesize(ctx: dict, state, memory) -> PromptLayer:
    task_id = ctx["task_id"]
    question = ctx["question"]
    sub_questions = ctx.get("sub_questions", [])
    evidence = ctx.get("evidence", [])
    experiments = ctx.get("experiments", [])
    # Findings already emitted by an earlier iteration. We pass them in so this
    # iteration can either *deepen* what's there with new angles or *amend*
    # something earlier, but doesn't re-derive the same fact a second time.
    prior_findings = ctx.get("prior_findings", [])

    task, theses = await _gather(state.get_task(task_id), state.get_active_theses(limit=10))

    thesis_lines = "\n".join(f"- T{t.id}: {t.claim}" for t in theses) or "(no active theses)"

    sub_q_lines = "\n".join(f"  [{i}] {sq}" for i, sq in enumerate(sub_questions))

    if evidence:
        ev_blocks = []
        for e in evidence:
            ev_blocks.append(
                f"  - [SQ{e['sub_question_idx']}, {e['stance']}, "
                f"conf {e['confidence']:.2f}] {e['url']}\n"
                f"    claim: {e['claim']}\n"
                f'    quote: "{e["quote"][:200]}"'
            )
        ev_block = "\n".join(ev_blocks)
    else:
        ev_block = "(no evidence collected)"

    if experiments:
        exp_blocks = []
        for x in experiments:
            interp = x.get("interpretation") or "(no interpretation)"
            exp_blocks.append(
                f"  - {x['kind']} (status: {x['status']})\n"
                f"    params: {json.dumps(x['params'])[:200]}\n"
                f"    interpretation: {interp[:400]}"
            )
        exp_block = "\n".join(exp_blocks)
    else:
        exp_block = "(no experiments run)"

    if prior_findings:
        pf_block = "\n".join(
            f"  - F{f['id']} ({f['source']}, rel {float(f['relevance_score']):.1f}): "
            f"{f['title']}\n    {f['summary'][:200]}"
            for f in prior_findings[:8]
        )
        pf_section = (
            f"\n## Findings already emitted in earlier iterations "
            f"({len(prior_findings)})\n"
            f"{pf_block}\n\n"
            "**Do NOT re-emit any of the findings above** as a fresh finding "
            "in this iteration. You may write a *new* finding that goes "
            "deeper, contradicts one of these, or covers a NEW angle the prior "
            "ones missed. Repeating the same claim from the same source is "
            "wasted work — skip it.\n"
        )
    else:
        pf_section = ""

    content = f"""## Research task
**Task:** {task.description}
**Framing question:** {question}
**Target thesis:** {f"T{task.thesis_id}" if task.thesis_id else "(exploratory)"}

## Sub-questions investigated
{sub_q_lines}

## Active theses (score findings for relevance to these)
{thesis_lines}

## Evidence collected ({len(evidence)} items)
{ev_block}

## Experiments run ({len(experiments)})
{exp_block}
{pf_section}
---

Synthesize the evidence into 1-4 **findings**. Each finding:
- `source`: one of hacker_news | arxiv | reddit | web | other
- `url`, `title`, `summary` (≤ 200 words, concrete and specific)
- `relevance_score` 1-10 (most should land 3-5; reserve 8+ for genuinely
  load-bearing findings backed by ≥2 evidence items)
- `supports_thesis`: true | false | null. Be calibrated. Null when the
  evidence is informational but doesn't tilt the thesis.
- `why_it_matters`: one sentence, concrete

Also return:
- `summary`: 2-4 sentence answer to the framing question
- `weakest_subquestion_idx`: which sub-question has the least / weakest
  evidence (or -1 if all are well-supported)
- `open_questions`: questions that remain after this pass

If the evidence is thin or contradictory, prefer **fewer findings** with
honest confidence over a long list of vague ones. An empty findings list
beats slop.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_gap_check(ctx: dict, state, memory) -> PromptLayer:
    question = ctx["question"]
    sub_questions = ctx.get("sub_questions", [])
    evidence = ctx.get("evidence", [])
    synthesis = ctx["synthesis"]
    iteration = ctx.get("iteration", 1)
    max_iterations = ctx.get("max_iterations", 2)

    sub_q_lines = "\n".join(f"  [{i}] {sq}" for i, sq in enumerate(sub_questions))

    ev_by_sq: dict[int, int] = {}
    for e in evidence:
        ev_by_sq[e["sub_question_idx"]] = ev_by_sq.get(e["sub_question_idx"], 0) + 1
    coverage = "\n".join(f"  [{i}]: {ev_by_sq.get(i, 0)} items" for i in range(len(sub_questions)))

    content = f"""## Framing question
{question}

## Sub-questions
{sub_q_lines}

## Evidence coverage per sub-question
{coverage}

## Synthesis just produced
{synthesis["summary"]}

**Open questions flagged:** {synthesis.get("open_questions") or "(none)"}
**Weakest sub-question:** {synthesis.get("weakest_subquestion_idx", -1)}

## Iteration budget
This was iteration {iteration} of {max_iterations}.

---

Decide whether another iteration is worth it:
- `has_gaps`: true if the synthesis leaves load-bearing questions unanswered.
- `gaps`: short list of what's missing.
- `should_iterate`: true ONLY if (a) gaps are real, (b) we have iteration
  budget left ({iteration} < {max_iterations}), and (c) you can name concrete
  follow-up sub-questions that would resolve them.
- `proposed_followups`: up to 3 new SubQuestions for the next pass.
- `reason`: one sentence why iterate / why stop.

Stopping is usually the right call. Iterating costs time. If the synthesis
is honest about its uncertainty, that may be the answer — don't iterate just
because the answer is "we don't fully know."
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_interpret_experiment(ctx: dict, state, memory) -> PromptLayer:
    kind = ctx["kind"]
    params = ctx["params"]
    result = ctx["result"]
    question = ctx["question"]
    sub_questions = ctx.get("sub_questions", [])

    sub_q_lines = "\n".join(f"  [{i}] {sq}" for i, sq in enumerate(sub_questions))

    content = f"""## Experiment run
**Kind:** {kind}
**Params:** {json.dumps(params)}

## Result
```json
{json.dumps(result, indent=2)[:6000]}
```

## Original framing question
{question}

## Sub-questions
{sub_q_lines}

---

Interpret the experiment result against the question and sub-questions:
- `summary`: 2-4 sentences. What does this tell us? Be concrete — name
  the numbers, the names, the comparison.
- `bears_on_subquestion_idxs`: which sub-questions this result speaks to.
- `confidence`: how informative is this result? 0..1.

If the experiment failed or returned empty, say so plainly. A null result
is also data.

**Important distinction.** Data-quality failures DO NOT support claims about
the topic:
- A repo 404 in `compare_repo_growth` means *that name doesn't exist on
  GitHub* — it is NOT evidence that the topic isn't adopted. Note the
  failure as a data issue, then interpret the OTHER repos in the result.
- An empty `count_demand_signal` result means *those phrases didn't match*
  — try variants in a follow-up; don't conclude the topic is unpopular.
- An empty `gh_search_trend` window can just mean the search qualifiers
  were too narrow. Same caveat.

Confidence should drop when the result is mostly data-quality noise.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# Helper: thin wrapper so calls to state methods can be parallelized cleanly.
async def _gather(*coros):
    import asyncio

    return await asyncio.gather(*coros)


# -------------------------------------------------------------------------
# Recipe registration (idempotent — guard against double-import)
# -------------------------------------------------------------------------

_RESEARCH_RECIPES: list[tuple[str, str, int, str, callable]] = [
    (
        "researcher.plan_inquiry",
        "Decompose a research task into sub-questions and experiments.",
        6_000,
        "InquiryPlan",
        _build_plan_inquiry,
    ),
    (
        "researcher.extract_evidence",
        "Extract verbatim quote-based evidence from one fetched page.",
        16_000,
        "EvidenceBatch",
        _build_extract_evidence,
    ),
    ("researcher.synthesize", "Synthesize evidence + experiments into findings.", 12_000, "Synthesis", _build_synthesize),
    (
        "researcher.gap_check",
        "Decide whether to iterate; propose follow-up sub-questions.",
        5_000,
        "GapCheck",
        _build_gap_check,
    ),
    (
        "researcher.interpret_experiment",
        "Interpret an experiment result against the framing question.",
        6_000,
        "ExperimentInterpretation",
        _build_interpret_experiment,
    ),
]

for _itype, _desc, _budget, _schema, _builder in _RESEARCH_RECIPES:
    if _itype not in RECIPES:
        RECIPES[_itype] = Recipe(
            invocation_type=_itype,
            description=_desc,
            agent="researcher",
            total_budget=_budget,
            use_cold_path=False,
            recall_sessions=[],
            recall_k=0,
            output_schema=_schema,
            task_data_builder=_builder,
        )


# -------------------------------------------------------------------------
# Orchestrator
# -------------------------------------------------------------------------

MAX_ITERATIONS = 2
MAX_PAGES_PER_SUBQ = 4  # safety cap on fetches per sub-question
MAX_EVIDENCE_TO_SYNTH = 60  # cap how much evidence we ship into synthesis


async def run_research_task(task, dispatcher, *, triggered_by_event_id=None) -> dict:
    """
    Drive the agentic researcher loop for one claimed task.

    Returns a dict with the run summary (inquiry ids, evidence count, finding
    ids, iterations, agent_run ids). The caller is responsible for marking
    the task complete via state.complete_task.

    Every LLM step is routed through the dispatcher's per-handler Session so
    /trace can render the run as a DAG. Per-page extracts and per-experiment
    interpretations fan out from their iteration's plan step (explicit
    parent_step_id) rather than chaining linearly.
    """
    state = dispatcher.state
    router = dispatcher.router
    curator = dispatcher.curator
    session = dispatcher.session  # may be None when called outside a handler context

    findings_emitted: list[int] = []
    all_evidence: list[dict] = []  # rolling, fed into synthesis + iter-2 plan
    experiments_run: list[dict] = []
    inquiry_ids: list[int] = []
    iteration = 1
    current_question = task.description

    while iteration <= MAX_ITERATIONS:
        # ---- 1. plan_inquiry -------------------------------------------
        plan_prompt = await curator.build(
            invocation_type="researcher.plan_inquiry",
            context={
                "task_id": task.id,
                "question": current_question,
                "iteration": iteration,
                "prior_evidence": all_evidence,
            },
        )
        plan, plan_run_id = await router.invoke(
            prompt=plan_prompt,
            output_schema_class=InquiryPlan,
            triggered_by_event_id=triggered_by_event_id,
            session=session,
            step_name=f"plan_inquiry/iter{iteration}",
        )

        inquiry_id = await state.record_inquiry(
            task_id=task.id,
            iteration=iteration,
            question=current_question,
            sub_questions=[sq.model_dump() for sq in plan.sub_questions],
            proposed_experiments=[pe.model_dump() for pe in plan.proposed_experiments],
            plan_run_id=plan_run_id,
        )
        inquiry_ids.append(inquiry_id)
        log.info(
            "research T%s iter %s: planned %d sub-questions, %d experiments",
            task.id,
            iteration,
            len(plan.sub_questions),
            len(plan.proposed_experiments),
        )

        # ---- 2. search + fetch + extract per sub-question --------------
        for sq_idx, sub_q in enumerate(plan.sub_questions):
            search_results = await _search_for_sub_question(
                sub_q.q,
                sub_q.sources,
                sub_q.k,
            )
            urls = [r.url for r in search_results[:MAX_PAGES_PER_SUBQ]]
            if not urls:
                log.info("research T%s SQ%s: no search results", task.id, sq_idx)
                continue

            pages = await web_fetch_many(urls, state, concurrency=4)
            url_to_title = {r.url: r.title for r in search_results}

            for page in pages:
                if page is None or not page.content.strip():
                    continue
                extract_prompt = await curator.build(
                    invocation_type="researcher.extract_evidence",
                    context={
                        "task_id": task.id,
                        "sub_question": sub_q.q,
                        "url": page.url,
                        "title": url_to_title.get(page.url, ""),
                        "content": page.content,
                    },
                )
                try:
                    batch, extract_run_id = await router.invoke(
                        prompt=extract_prompt,
                        output_schema_class=EvidenceBatch,
                        triggered_by_event_id=triggered_by_event_id,
                        session=session,
                        # Fan-out from the plan step, not linear-chained, so
                        # /trace shows per-page extracts as siblings under the
                        # plan rather than a 50-deep chain.
                        step_name=f"extract_evidence/iter{iteration}/sq{sq_idx}",
                        parent_step_id=plan_run_id,
                    )
                except Exception as e:  # noqa: BLE001 — per-page failure is non-fatal
                    log.warning("extract failed for %s: %s", page.url, e)
                    continue

                for ev in batch.evidence:
                    ev_id = await state.record_evidence(
                        task_id=task.id,
                        inquiry_id=inquiry_id,
                        sub_question_idx=sq_idx,
                        url=page.url,
                        title=url_to_title.get(page.url),
                        quote=ev.quote,
                        claim=ev.claim,
                        stance=ev.stance,
                        confidence=ev.confidence,
                        extract_run_id=extract_run_id,
                    )
                    all_evidence.append(
                        {
                            "id": ev_id,
                            "sub_question_idx": sq_idx,
                            "url": page.url,
                            "title": url_to_title.get(page.url, ""),
                            "quote": ev.quote,
                            "claim": ev.claim,
                            "stance": ev.stance,
                            "confidence": ev.confidence,
                        }
                    )

        # ---- 3. experiments + interpret --------------------------------
        if plan.proposed_experiments:
            experiments_run.extend(
                await _run_experiments(
                    plan.proposed_experiments,
                    task=task,
                    inquiry_id=inquiry_id,
                    question=current_question,
                    sub_questions=[sq.q for sq in plan.sub_questions],
                    dispatcher=dispatcher,
                    triggered_by_event_id=triggered_by_event_id,
                    iteration=iteration,
                    plan_run_id=plan_run_id,
                )
            )

        # ---- 4. synthesize ---------------------------------------------
        # Pull what previous iterations already wrote so synth doesn't restate.
        prior_findings_for_synth: list[dict] = []
        if findings_emitted:
            prior_rows = await state.get_findings(findings_emitted)
            prior_findings_for_synth = [
                {
                    "id": f.id,
                    "source": f.source or "other",
                    "title": f.title or "",
                    "summary": f.summary,
                    "relevance_score": float(f.relevance_score),
                }
                for f in prior_rows
            ]

        synth_prompt = await curator.build(
            invocation_type="researcher.synthesize",
            context={
                "task_id": task.id,
                "question": current_question,
                "sub_questions": [sq.q for sq in plan.sub_questions],
                "evidence": all_evidence[-MAX_EVIDENCE_TO_SYNTH:],
                "experiments": experiments_run,
                "prior_findings": prior_findings_for_synth,
            },
        )
        synthesis, synth_run_id = await router.invoke(
            prompt=synth_prompt,
            output_schema_class=Synthesis,
            triggered_by_event_id=triggered_by_event_id,
            session=session,
            step_name=f"synthesize/iter{iteration}",
            # Synthesis pulls from all the fan-out work in this iteration; its
            # logical parent is the plan that scoped the iteration.
            parent_step_id=plan_run_id,
        )

        for f in synthesis.findings:
            fid = await state.record_finding(
                task_id=task.id,
                thesis_id=task.thesis_id,
                source=f.source,
                url=f.url,
                title=f.title,
                summary=f.summary,
                relevance_score=f.relevance_score,
                why_it_matters=f.why_it_matters,
                supports_thesis=f.supports_thesis,
            )
            findings_emitted.append(fid)

        # ---- 5. gap_check + iterate? -----------------------------------
        if iteration >= MAX_ITERATIONS:
            break

        gap_prompt = await curator.build(
            invocation_type="researcher.gap_check",
            context={
                "task_id": task.id,
                "question": current_question,
                "sub_questions": [sq.q for sq in plan.sub_questions],
                "evidence": all_evidence,
                "synthesis": synthesis.model_dump(),
                "iteration": iteration,
                "max_iterations": MAX_ITERATIONS,
            },
        )
        gap, gap_run_id = await router.invoke(
            prompt=gap_prompt,
            output_schema_class=GapCheck,
            triggered_by_event_id=triggered_by_event_id,
            session=session,
            step_name=f"gap_check/iter{iteration}",
            parent_step_id=synth_run_id,
        )

        if not gap.should_iterate or not gap.proposed_followups:
            break

        # Build the next iteration's framing from the proposed follow-ups.
        current_question = f"Follow-up on '{task.description}'. Gaps to close: " + "; ".join(gap.gaps[:3])
        iteration += 1

    return {
        "iterations": iteration,
        "inquiry_ids": inquiry_ids,
        "evidence_count": len(all_evidence),
        "experiments_run": len(experiments_run),
        "findings": findings_emitted,
    }


# -------------------------------------------------------------------------
# Experiments runner — used by Phase 3, defined here for locality
# -------------------------------------------------------------------------


async def _run_experiments(
    proposed,
    *,
    task,
    inquiry_id,
    question,
    sub_questions,
    dispatcher,
    triggered_by_event_id,
    iteration,
    plan_run_id,
) -> list[dict]:
    """
    Dispatch each proposed experiment, persist a row, then ask the model to
    interpret the result. Returns the list of completed experiment dicts
    (kind, params, status, result, interpretation) suitable for synthesis.

    Interpret steps fan out from the iteration's plan_run_id, matching the
    extract_evidence parentage so /trace shows experiments as plan siblings.
    """
    from labfoundry.research.experiments import dispatch as exp_dispatch

    state = dispatcher.state
    router = dispatcher.router
    curator = dispatcher.curator
    session = dispatcher.session

    completed: list[dict] = []
    for exp_idx, proposal in enumerate(proposed):
        exp_id = await state.start_experiment(
            task_id=task.id,
            inquiry_id=inquiry_id,
            kind=proposal.kind,
            params=proposal.params,
        )
        try:
            result = await exp_dispatch(proposal.kind, proposal.params, dispatcher=dispatcher)
        except Exception as e:  # noqa: BLE001 — single experiment failure is non-fatal
            log.warning("experiment %s failed: %s", proposal.kind, e)
            await state.fail_experiment(exp_id, str(e))
            completed.append(
                {
                    "id": exp_id,
                    "kind": proposal.kind,
                    "params": proposal.params,
                    "status": "failed",
                    "result": None,
                    "error": str(e),
                    "interpretation": None,
                }
            )
            continue

        # Interpret
        interp_prompt = await curator.build(
            invocation_type="researcher.interpret_experiment",
            context={
                "kind": proposal.kind,
                "params": proposal.params,
                "result": result,
                "question": question,
                "sub_questions": sub_questions,
            },
        )
        interp, interp_run_id = await router.invoke(
            prompt=interp_prompt,
            output_schema_class=ExperimentInterpretation,
            triggered_by_event_id=triggered_by_event_id,
            session=session,
            step_name=f"interpret_experiment/iter{iteration}/{proposal.kind}#{exp_idx}",
            parent_step_id=plan_run_id,
        )
        await state.complete_experiment(
            experiment_id=exp_id,
            result=result,
            interpretation=interp.summary,
            interpret_run_id=interp_run_id,
        )

        completed.append(
            {
                "id": exp_id,
                "kind": proposal.kind,
                "params": proposal.params,
                "status": "completed",
                "result": result,
                "interpretation": interp.summary,
                "bears_on_subquestion_idxs": interp.bears_on_subquestion_idxs,
                "confidence": interp.confidence,
            }
        )

    return completed
