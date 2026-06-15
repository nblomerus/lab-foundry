"""
Context curator.

Owns exactly what each agent invocation sees. Agents request work by
`invocation_type`; the curator looks up the recipe, fetches recall from Zep,
applies lessons, enforces the token budget, and returns a Prompt ready
for the router to execute.

All methods are async because state/memory/lessons clients are async.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import tiktoken

from agents.identity import persona_for  # agent persona registry (migration 024); leaf util, no cycle

# -------------------------------------------------------------------------
# Layered prompt
# -------------------------------------------------------------------------


@dataclass
class PromptLayer:
    name: str
    content: str
    priority: int  # 0 = never drop; higher = compactable / droppable first

    def token_count(self, tokenizer) -> int:
        return len(tokenizer.encode(self.content))


@dataclass
class BuiltPrompt:
    layers: list[PromptLayer]
    tool_names: list[str]
    output_schema: str
    lesson_ids: list[int]
    total_tokens: int
    invocation_type: str

    def as_system_message(self) -> str:
        return "\n\n".join(layer.content for layer in self.layers if layer.content)


# -------------------------------------------------------------------------
# Recipe definition
# -------------------------------------------------------------------------

# task_data_builder signature: async (ctx, state, memory) -> PromptLayer
TaskDataBuilder = Callable[[dict, object, object], Awaitable[PromptLayer]]


@dataclass
class Recipe:
    invocation_type: str
    description: str
    agent: str
    total_budget: int
    use_cold_path: bool = False
    recall_sessions: list[str] = field(default_factory=list)
    recall_k: int = 5
    output_schema: str = ""
    task_data_builder: TaskDataBuilder | None = None


# -------------------------------------------------------------------------
# System prompts — terse role anchors
# -------------------------------------------------------------------------

SYSTEM_PROMPTS: dict[str, str] = {
    "pi": (
        "You are the Principal Investigator of an autonomous AI-native research lab. "
        "Your role is research direction, not execution. You select which hypotheses to pursue, "
        "which claims to kill, and when to change direction. You are demanding about rigour and "
        "ruthlessly selective about what constitutes evidence. You write research decisions; "
        "you never write content."
    ),
    "knowledge_scout": (
        "You are a research analyst. You read raw material and extract "
        "findings that inform research decisions. You are precise, "
        "skeptical, and selective. Most material is not interesting. "
        "Empty findings lists are acceptable. Inflating relevance scores "
        "is worse than under-scoring."
    ),
    "evaluation": (
        "You are the Evaluation Division. Your job is to detect slop, fabricated evidence, "
        "metric misrepresentation, data leakage, and statistical overreach. Every finding must "
        "earn its confidence score. A single fabricated claim contaminates an entire finding."
    ),
    "critic": (
        "You hunt contradictions, novelty gaps, baseline fairness failures, data leakage, "
        "cherry-picking, and over-claiming. Your job is to kill weak claims before they waste "
        "compute. A claim that survives your attack is the only one worth advancing."
    ),
    "planner": (
        "You translate claims into concrete research tasks. Each task must be falsifiable, "
        "scoped to one claim, and produceable by a knowledge scout in a single session."
    ),
    "mimir": (
        "You are Mimir, Warden of the Library. You decide whether a source is trustworthy "
        "enough to enter the research corpus. Most trust is settled deterministically; you are "
        "consulted ONLY for ambiguous web sources with no verifiable identifier. Approve "
        "credible, citable sources; block spam, SEO filler, and unverifiable claims. You may "
        "not mint paper-grade trust — that requires a verifiable identifier you don't have here."
    ),
}


# -------------------------------------------------------------------------
# Recipe 1: pi.claim_verdict
# -------------------------------------------------------------------------


async def _build_thesis_kill_task_data(ctx: dict, state, memory) -> PromptLayer:
    thesis_id = ctx["thesis_id"]
    verdict_id = ctx["adversary_verdict_id"]

    thesis, verdict, siblings = await asyncio.gather(
        state.get_thesis(thesis_id),
        state.get_adversary_verdict(verdict_id),
        state.get_active_theses(limit=20, exclude_ids=[thesis_id]),
    )
    cited_findings = await state.get_findings(ids=verdict.cited_finding_ids)

    sibling_lines = (
        "\n".join(f"- T{t.id}: {t.claim} (conf {t.confidence:.2f}, status {t.status})" for t in siblings) or "(none)"
    )

    cited_lines = "\n".join(
        f"- F{f.id} [{f.source}, rel {f.relevance_score}]: {f.title}\n  {f.summary}" for f in cited_findings
    )

    content = f"""## Target thesis under kill review

**Claim:** {thesis.claim}
**Status:** {thesis.status}  |  Confidence: {thesis.confidence:.2f}
**Born:** {thesis.created_at:%Y-%m-%d}

## Critic verdict (the kill recommendation)

**Verdict:** {verdict.verdict}  |  Confidence: {verdict.confidence:.2f}
**Reasoning:**
{verdict.reasoning}

## Cited killing evidence ({len(cited_findings)} findings)
{cited_lines}

## Sibling theses (do not act on these in this run)
{sibling_lines}

---

**Decide one of:**
  - `kill`         — the verdict is right. Write a 1-2 sentence kill_reason for the permanent record.
  - `demote`       — the evidence weakens but does not kill. Propose new_confidence.
  - `reject`       — the critic missed something. Explain what.

Your decision will be logged immutably. The Critic is good but not infallible.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe 2: researcher.execute_task
# -------------------------------------------------------------------------


async def _build_researcher_task_data(ctx: dict, state, memory) -> PromptLayer:
    task_id = ctx["task_id"]
    raw_material = ctx.get("raw_material", "")  # caller pre-fetches via MCP

    task, theses = await asyncio.gather(
        state.get_task(task_id),
        state.get_active_theses(limit=10),
    )

    thesis_lines = "\n".join(f"- T{t.id}: {t.claim}" for t in theses) or "(no active theses — exploration just started)"

    content = f"""## Research task

**Task:** {task.description}
**Type:** {task.task_type}
**Target thesis:** T{task.thesis_id if task.thesis_id else "(none — exploratory)"}

## Active theses (score findings for relevance to these)
{thesis_lines}

## Raw material to analyze
{raw_material}

---

Emit 0 to N findings. For each finding:
  - `source`: one of hacker_news | arxiv | reddit | web | other
  - `url`, `title`, `summary` (≤ 200 words)
  - `relevance_score` (1-10): most = 3-5, reserve 8+ for genuinely important findings
  - `supports_thesis`: true | false | null  (null = neutral / informational only)
  - `why_it_matters`: one sentence, concrete

ONLY emit a finding if it carries SPECIFIC, checkable signal: a number or statistic,
a price, a named company/competitor, a dated event, or a real user complaint or
demand quote. SKIP — do NOT emit — generic overviews, "guide"/"best practices"/
"everything you need to know" explainers, vendor marketing, and anything that merely
describes what something is. A generic explainer is NOT a finding; it is slop.

If the raw material contains nothing specific, return an empty list.
That is the correct answer when there is nothing — an empty list beats a vague one.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe 3: pi.exploration_kickoff  (bootstrap — day 1 only)
# -------------------------------------------------------------------------


async def _build_exploration_kickoff_task_data(ctx: dict, state, memory) -> PromptLayer:
    """
    First-ever PI invocation. The constitution layer already carries the seed
    (problem / stance / success criterion); this layer is the "now what" brief.
    """
    content = """## Exploration kickoff — framing the research agenda

You are bootstrapping the lab from the seed above. There is no deadline; the
lab is judged on the rigour and novelty of what it eventually establishes, not
on speed. Do not impose or assume a timeline.

Right now your ONLY job is to propose 4-6 candidate **research directions** in
machine learning / AI worth investigating against the Library (a large corpus of
arXiv papers). A direction is NOT a single experiment — it is a line of inquiry
the lab could pursue, framed so that evidence could move it. Examples
(illustrative only — do not use these):
  - "Retrieval quality is bottlenecked more by chunking/representation than by
    the reranker, across long-document QA"
  - "Small models with tool use match larger models on agentic benchmarks once
    the scaffold is held fixed"
  - "A specific, under-tested failure mode in mixture-of-experts routing"

For each direction:
  - `claim`: one sentence stating the direction as a falsifiable thesis
  - `rationale`: 2-3 sentences. Why does this matter, and why is it
    under-explored or contested? Where is the leverage?
  - `risks`: 1-2 sentences. What would make this a dead end — already settled,
    not measurable, or confounded?
  - `disambiguating_questions`: exactly 3 questions whose answers tell us
    whether the direction is real and tractable. These become the first
    research tasks.

Hard requirements:
  - **Distinct.** No two directions should be the same inquiry in different words.
  - **Evidence-grounded.** Each must be answerable from the literature and/or a
    runnable experiment — not a matter of opinion or a survey.
  - **Stance-compatible.** No hype, no incremental deltas dressed as
    breakthroughs, no survey-only directions. Reread the stance.
  - **Falsifiable.** A clear result (either way) must be conceivable.

Return 4-6 directions. Five is the right number unless you have a specific
reason for more or fewer. Also return a 2-3 sentence `selection_reasoning`
explaining what space these directions cover and what you deliberately excluded.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


async def _build_mimir_certify_task_data(ctx: dict, state, memory) -> PromptLayer:
    content = f"""## Source under trust review

Title: {ctx.get("title") or "(none)"}
URL:   {ctx.get("source_url") or "(none)"}
Host:  {ctx.get("host") or "(unknown)"}

This source has NO falsifiable trust identifier (not arXiv, no resolving DOI, not
an active GitHub repo, not a known-reputable domain). Decide whether it belongs
in the research corpus.

- decision: "approve" for a credible, citable source; "block" for spam / SEO
  filler / content farms / unverifiable claims.
- tier: user_asserted | web_unknown | web_reputable (you may NOT set a higher
  tier — paper-grade trust needs a verifiable identifier). Default to web_unknown
  unless the source is clearly reputable.
- reasons: 1-2 sentences.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe registry
# -------------------------------------------------------------------------

RECIPES: dict[str, Recipe] = {
    "pi.exploration_kickoff": Recipe(
        invocation_type="pi.exploration_kickoff",
        description="First-ever PI invocation. Generates 4-6 candidate research directions from the seed.",
        agent="pi",
        total_budget=4_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ExplorationKickoffOutput",
        task_data_builder=_build_exploration_kickoff_task_data,
    ),
    "pi.claim_verdict": Recipe(
        invocation_type="pi.claim_verdict",
        description="PI ratifies, demotes, or rejects an Critic kill recommendation.",
        agent="pi",
        total_budget=13_000,
        use_cold_path=True,
        recall_sessions=["claims-lifecycle", "dissent", "pi-deliberations"],
        recall_k=10,
        output_schema="ThesisKillDecision",
        task_data_builder=_build_thesis_kill_task_data,
    ),
    "researcher.execute_task": Recipe(
        invocation_type="researcher.execute_task",
        description="Researcher analyzes raw material and emits findings against active theses.",
        agent="researcher",
        total_budget=22_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="ResearcherFindings",
        task_data_builder=_build_researcher_task_data,
    ),
    "mimir.certify": Recipe(
        invocation_type="mimir.certify",
        description="Mimir's trust tie-breaker for an ambiguous web source with no verifiable identifier.",
        agent="mimir",
        total_budget=6_000,
        use_cold_path=True,
        recall_sessions=[],
        recall_k=0,
        output_schema="MimirVerdict",
        task_data_builder=_build_mimir_certify_task_data,
    ),
}


# -------------------------------------------------------------------------
# Tool filtering per agent
# -------------------------------------------------------------------------

TOOLS_BY_AGENT: dict[str, list[str]] = {
    "pi": ["labfoundry-state", "labfoundry-memory", "labfoundry-events", "labfoundry-artifacts"],
    "planner": ["labfoundry-state", "labfoundry-events"],
    "researcher": ["labfoundry-state", "labfoundry-events", "labfoundry-research"],
    "evaluation": ["labfoundry-state", "labfoundry-memory", "labfoundry-events"],
    "adversary": ["labfoundry-state", "labfoundry-memory", "labfoundry-events", "labfoundry-research"],
    "mimir": ["labfoundry-state", "labfoundry-corpus", "labfoundry-knowledge"],
}


# -------------------------------------------------------------------------
# The Curator
# -------------------------------------------------------------------------


class Curator:
    """
    Builds the full layered context for one invocation.

    Layer order (priority 0 layers are never dropped on overflow):
        0  system           — role anchor
        0  constitution     — seed problem | charter
        0  schema hint      — output schema instruction
        1  phase            — where we are, time budget
        1  task_data        — recipe-specific
        2  lessons          — applicable skill memory
        3  recall           — Zep hot/cold (compacted on overflow)
    """

    def __init__(self, state, memory, lessons, tokenizer=None):
        self.state = state
        self.memory = memory
        self.lessons = lessons
        self.tokenizer = tokenizer or tiktoken.get_encoding("cl100k_base")

    async def build(self, invocation_type: str, context: dict) -> BuiltPrompt:
        recipe = RECIPES.get(invocation_type)
        if recipe is None:
            raise ValueError(f"No recipe for invocation_type={invocation_type!r}")

        # Build static + recall layers concurrently where possible
        constitution_task = asyncio.create_task(self._constitution_layer())
        phase_task = asyncio.create_task(self._phase_layer())
        lessons_task = asyncio.create_task(self._lessons_layer(invocation_type, context))

        recall_task = None
        if recipe.recall_sessions:
            recall_task = asyncio.create_task(self._recall_layer(recipe, context))

        task_data_task = None
        if recipe.task_data_builder:
            task_data_task = asyncio.create_task(recipe.task_data_builder(context, self.state, self.memory))

        # Resolve the agent's system persona from the identity registry (migration 024), falling back
        # to the code SYSTEM_PROMPTS constant when there is no row (so a missing identity never breaks
        # the prompt). persona_for is cached + never raises.
        pool = getattr(self.state, "pool", None)
        persona = (await persona_for(pool, recipe.agent)) if pool is not None else None
        system_content = persona or SYSTEM_PROMPTS.get(recipe.agent, "")

        layers: list[PromptLayer] = [
            PromptLayer(
                name="system",
                content=system_content,
                priority=0,
            ),
            await constitution_task,
            await phase_task,
        ]

        lessons_layer, applied_ids = await lessons_task
        if lessons_layer.content:
            layers.append(lessons_layer)

        if recall_task is not None:
            layers.append(await recall_task)

        if task_data_task is not None:
            layers.append(await task_data_task)

        layers.append(
            PromptLayer(
                name="schema",
                content=f"Return a JSON object matching the {recipe.output_schema} schema.",
                priority=0,
            )
        )

        layers = self._enforce_budget(layers, recipe.total_budget)
        total = sum(layer.token_count(self.tokenizer) for layer in layers)

        return BuiltPrompt(
            layers=layers,
            tool_names=TOOLS_BY_AGENT.get(recipe.agent, []),
            output_schema=recipe.output_schema,
            lesson_ids=applied_ids,
            total_tokens=total,
            invocation_type=invocation_type,
        )

    # ---- Layer builders ---------------------------------------------

    async def _constitution_layer(self) -> PromptLayer:
        s = await self.state.get_company_state()
        # Stage 0 (market-PI neutralized): the constitution always grounds agents in
        # the seed problem. The old execution-phase branch injected company_state.charter
        # — a committed MARKET thesis the phase-transition PI wrote AUTONOMOUSLY — into
        # every agent's prompt (priority 0). Disabled so a stale or auto-written charter
        # can't hijack the lab's direction. Re-enable only with an explicit, human-gated
        # charter path if the market-lifecycle is ever intentionally reinstated.
        body = (
            f"## Seed problem\n{s.problem_statement}\n\n"
            f"## Stance\n{s.stance or '(unset)'}\n\n"
            f"## Success criterion\n{s.success_criterion or '(unset)'}"
        )
        return PromptLayer(name="constitution", content=body, priority=0)

    async def _phase_layer(self) -> PromptLayer:
        s, active = await asyncio.gather(
            self.state.get_company_state(),
            self.state.count_active_theses(),
        )
        now = datetime.now(UTC)
        days_in_phase = (now - s.phase_started_at).days
        days_since_start = (now - s.bootstrap_at).days
        body = (
            f"## Phase context\n"
            f"Phase: **{s.current_phase}** (day {days_in_phase})\n"
            f"Days since start: {days_since_start}\n"
            f"Active theses: {active}"
        )
        return PromptLayer(name="phase", content=body, priority=1)

    async def _lessons_layer(
        self,
        invocation_type: str,
        context: dict,
    ) -> tuple[PromptLayer, list[int]]:
        applicable = await self.lessons.fetch_applicable(
            invocation_type=invocation_type,
            context=context,
            limit=5,
        )
        if not applicable:
            return PromptLayer(name="lessons", content="", priority=2), []

        lines, ids = [], []
        for lesson in applicable:
            marker = "(verified)" if lesson.status == "active" else "(unverified)"
            lines.append(f"- {marker} {lesson.lesson_text}")
            ids.append(lesson.id)

        body = "## Learned lessons (apply when relevant)\n" + "\n".join(lines)
        return PromptLayer(name="lessons", content=body, priority=2), ids

    async def _recall_layer(self, recipe: Recipe, context: dict) -> PromptLayer:
        query = await self._recall_query(recipe, context)
        if recipe.use_cold_path:
            sessions = await asyncio.gather(
                *[
                    self.memory.recall_episodic(session_id=sid, query=query, k=recipe.recall_k)
                    for sid in recipe.recall_sessions
                ]
            )
        else:
            sessions = await asyncio.gather(
                *[self.memory.recent(session_id=sid, k=recipe.recall_k) for sid in recipe.recall_sessions]
            )
        results = [m for batch in sessions for m in batch]

        if not results:
            body = "## Recall\n(no relevant prior episodes)"
        else:
            lines = [f"[{m.created_at:%Y-%m-%d %H:%M}] {m.content}" for m in results]
            body = "## Recall (relevant prior episodes)\n" + "\n".join(lines)

        return PromptLayer(name="recall", content=body, priority=3)

    async def _recall_query(self, recipe: Recipe, context: dict) -> str:
        if recipe.invocation_type == "pi.claim_verdict":
            thesis = await self.state.get_thesis(context["thesis_id"])
            return f"thesis kill, demotion, or rejection related to: {thesis.claim}"
        if recipe.invocation_type == "pi.weekly_synthesis":
            return "recent thesis activity, dissent, phase progress"
        if recipe.invocation_type == "pi.phase_transition_ratify":
            return "phase transition reasoning and confidence patterns"
        if recipe.invocation_type == "evaluation.slop_score":
            return "recent evaluation verdicts and slop patterns"
        return ""

    # ---- Budget enforcement ----------------------------------------

    def _enforce_budget(self, layers: list[PromptLayer], budget: int) -> list[PromptLayer]:
        total = sum(layer.token_count(self.tokenizer) for layer in layers)
        if total <= budget:
            return layers

        # 1) Compact recall first
        for layer in layers:
            if layer.name == "recall":
                overflow = total - budget
                target = max(0, layer.token_count(self.tokenizer) - overflow)
                layer.content = self._compact_recall(layer.content, target_tokens=target)
                break

        total = sum(layer.token_count(self.tokenizer) for layer in layers)
        if total <= budget:
            return layers

        # 2) Drop priority >= 2 layers (lessons, recall) if still over
        for layer in layers:
            if layer.priority >= 2 and layer.content:
                layer.content = ""
                total = sum(layer.token_count(self.tokenizer) for layer in layers)
                if total <= budget:
                    break

        return layers

    def _compact_recall(self, content: str, target_tokens: int) -> str:
        """
        Stub. Real implementation: F-tier model call asking 'summarize
        preserving decisions, dissent, and dates; drop deliberation phrasing.'
        For now: naive truncation.
        """
        tokens = self.tokenizer.encode(content)
        if len(tokens) <= target_tokens:
            return content
        return self.tokenizer.decode(tokens[:target_tokens]) + "\n[…truncated]"
