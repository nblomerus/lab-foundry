"""
Model router.

Selects model tier per invocation_type, manages daily caps, serializes
through a single-Ollama GPU lock, invokes the model with structured output,
and persists the agent_run row.

Agents never name a model. They request invocations by invocation_type,
the router decides the tier, and the routing table is the only place
model selection happens.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import httpx
from pydantic import BaseModel

from harness.curator import BuiltPrompt
from harness.session import Session

log = logging.getLogger(__name__)


def _maybe_langfuse():
    """Initialize Langfuse if env vars are present; otherwise return None."""
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    if not pk or not sk:
        return None
    try:
        from langfuse import Langfuse

        client = Langfuse(public_key=pk, secret_key=sk, host=host)
        log.info("Langfuse client initialized → %s", host)
        return client
    except Exception as e:
        log.warning("Langfuse init failed (%s); continuing without tracing", e)
        return None


# -------------------------------------------------------------------------
# Tiers
# -------------------------------------------------------------------------


class Tier(enum.Enum):
    REASONING = "reasoning"
    WORKHORSE = "workhorse"
    FAST = "fast"
    CODE = "code"
    EXPERIMENT = "experiment"  # the experiments agent's code design/debug loop — premium (DeepSeek) lead


class Provider(enum.Enum):
    OLLAMA = "ollama"  # local, serialized behind the GPU lock
    GEMINI = "gemini"  # Google AI Studio, OpenAI-compatible
    GROQ = "groq"  # Groq LPU (fast open models), OpenAI-compatible
    NVIDIA = "nvidia"  # NVIDIA NIM catalog, OpenAI-compatible
    GITHUB = "github"  # GitHub Models (GPT-4o, Llama, …), OpenAI-compatible
    OPENAI = "openai"  # OpenAI direct (personal key, paid) — premium tier only
    DEEPSEEK = "deepseek"  # DeepSeek API (paid reasoning model) — premium tier lead


@dataclass(frozen=True)
class ModelSpec:
    tier: Tier
    model_name: str
    context_limit: int
    temperature: float
    cost_per_1k_input: float
    cost_per_1k_output: float
    provider: Provider = Provider.OLLAMA


# Local models (the fallback layer). Used when the cloud provider is
# disabled (no key) or fails / is rate-limited.
MODELS: dict[Tier, ModelSpec] = {
    Tier.REASONING: ModelSpec(
        tier=Tier.REASONING,
        model_name="deepseek-r1:32b-qwen-distill-q4_K_M",
        context_limit=16_000,
        temperature=0.3,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
    ),
    Tier.WORKHORSE: ModelSpec(
        tier=Tier.WORKHORSE,
        model_name="qwen3:14b",
        context_limit=32_000,
        temperature=0.4,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
    ),
    Tier.FAST: ModelSpec(
        tier=Tier.FAST,
        model_name="mistral:7b-instruct-q4_K_M",
        context_limit=8_000,
        temperature=0.2,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
    ),
    Tier.CODE: ModelSpec(
        tier=Tier.CODE,
        # qwen2.5-coder:7b chosen 2026-05-28 after benching researcher.extract_evidence
        # against qwen2.5:14b on a real fetched page:
        #   - 5-8x faster warm (3-5s vs 26-27s)
        #   - more calibrated: emits 3 high-confidence supports items vs 7-8 mid-confidence
        #     neutral items (the 14b is sycophantic-and-verbose on per-page extraction)
        #   - half the VRAM (4.4GB vs 8.4GB) — fits on the 2070 SUPER so calls can run
        #     in parallel across both GPUs (paired with the per-model lock change)
        model_name="qwen2.5-coder:7b",
        context_limit=32_000,
        temperature=0.2,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
    ),
    # The experiments agent writes + debugs real ML/AI experiment code — a complex,
    # iterative task. It leads with DeepSeek (premium, see PREMIUM_TIERS below) and
    # falls back to the local coder model when DeepSeek is capped/down.
    Tier.EXPERIMENT: ModelSpec(
        tier=Tier.EXPERIMENT,
        model_name="qwen2.5-coder:7b",
        context_limit=32_000,
        temperature=0.2,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
    ),
}

# -------------------------------------------------------------------------
# Cloud provider chain
#
# Each provider is a free, OpenAI-compatible endpoint serving a frontier-class
# (or large open) model. They're tried in order; a rate-limit / 5xx / timeout
# / unparseable output on one falls through to the next, with local Ollama as
# the final backstop. Spreading load across several free tiers multiplies
# effective free throughput and means a single provider's throttling is just
# a fall-through, not a degrade-to-local.
# -------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudProvider:
    provider: Provider
    base_url: str
    api_key: str
    model_name: str
    # "json_schema" = strict structured output (Gemini, NVIDIA NIM, OpenAI).
    # "json_object" = JSON syntax only; schema comes from the prompt + our
    # Pydantic validation + fallback (Groq doesn't support json_schema).
    structured_mode: str = "json_schema"
    # GPT-5.x / o-series reasoning models reject a custom temperature; omit it
    # for those so the request isn't rejected.
    send_temperature: bool = True


def build_cloud_chain(env: dict) -> list[CloudProvider]:
    """The free-cloud chain — INTENTIONALLY EMPTY under the lab policy of "only DeepSeek
    or local" (2026-06-08). Gemini / Groq / GitHub / NVIDIA are dropped; DeepSeek leads
    via build_premium_chain (the high-stakes tiers), and every tier falls back to local
    Ollama (MODELS[tier]). Volume tiers (FAST/CODE) therefore run local-only.
    """
    return []


# Tiers that lead with the premium chain (DeepSeek → OpenAI → GitHub) before the
# free chain. REASONING = the company's highest-stakes calls. WORKHORSE = the
# strategy/planning brain (planner.generate_tasks, PI synthesis/rescore/spawn,
# contradiction-hunt) — high-leverage and quality-sensitive, and cheap enough on
# DeepSeek (~$0.0006/call) to be worth it. FAST/CODE stay free-local (volume).
PREMIUM_TIERS = {Tier.REASONING, Tier.WORKHORSE, Tier.EXPERIMENT}


def build_premium_chain(env: dict) -> list[CloudProvider]:
    """The high-stakes lead chain for PREMIUM_TIERS. Under the "only DeepSeek or local"
    policy (2026-06-08) this is DeepSeek ONLY — OpenAI/GitHub dropped. PREMIUM_TIERS
    (REASONING/WORKHORSE) run DeepSeek → local; everything else is local (build_cloud_chain
    is empty). DeepSeek is cheap, reliable cloud; local Ollama is the always-up fallback.
    """
    chain: list[CloudProvider] = []
    if env.get("DEEPSEEK_API_KEY"):
        chain.append(
            CloudProvider(
                Provider.DEEPSEEK,
                "https://api.deepseek.com",
                env["DEEPSEEK_API_KEY"],
                env.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "json_object",
            )
        )
    return chain


# -------------------------------------------------------------------------
# Route table — single source of truth for "what runs this"
# -------------------------------------------------------------------------

ROUTE: dict[str, Tier] = {
    # Reasoning — high-stakes; capped at 50/day (cheap + reliable via DeepSeek)
    "pi.claim_verdict": Tier.REASONING,
    "pi.phase_transition_ratify": Tier.REASONING,
    "pi.charter_write": Tier.REASONING,
    "critic.kill_verdict": Tier.REASONING,
    # Workhorse — standard strategic / tactical
    "pi.exploration_kickoff": Tier.WORKHORSE,
    "mimir.certify": Tier.WORKHORSE,  # only the ~5% ambiguous web_unknown case; the deterministic path is free
    "pi.weekly_synthesis": Tier.WORKHORSE,
    "pi.rescore_claims": Tier.WORKHORSE,
    "pi.spawn_claim": Tier.WORKHORSE,
    "planner.generate_tasks": Tier.WORKHORSE,
    "critic.contradiction_hunt": Tier.WORKHORSE,
    # Fast — verifiers and high-volume classifiers
    # upgraded: slop gate needs a reliable, accurate model (DeepSeek), not 429→mistral:7b
    "evaluation.slop_score": Tier.WORKHORSE,
    "evaluation.relevance_verify": Tier.FAST,
    "phase_adjudicator.check": Tier.FAST,
    "curator.compact_recall": Tier.FAST,
    "reflect.lesson_propose": Tier.FAST,
    # Hinge A of the learning loop (LESSON_JUDGE=on): score whether applied
    # lessons helped a run. FAST — a bounded, low-token judgment.
    "reflect.judge_applications": Tier.FAST,
    # Batch reflection (REFLECTION_LOOP=v2): scans a window of dissents and
    # surfaces recurring patterns. WORKHORSE because spotting cross-run
    # patterns is more demanding than a single-run lesson judgment.
    "reflect.batch_propose_lessons": Tier.WORKHORSE,
    # Code — tool-using extractors
    "researcher.execute_task": Tier.CODE,  # legacy single-shot (kept for fallback)
    "researcher.summarize_source": Tier.CODE,
    # Agentic researcher loop (replaces the legacy single-shot when
    # RESEARCHER_LOOP != 'legacy'). plan / synthesize / gap_check / interpret
    # need reasoning + multi-source synthesis (WORKHORSE, DeepSeek-led).
    # extract_evidence is per-page + high-volume; it WAS CODE (local qwen2.5-coder),
    # but the local model serializes on the per-model GPU lock, so concurrent
    # researchers queued there — the bottleneck to running a pool of researchers in
    # parallel. Moved to WORKHORSE (DeepSeek-led, local qwen3:14b fallback) so the
    # whole researcher loop is cloud and N researchers truly parallelize. Bumped
    # DAILY_CAPS[WORKHORSE] to cover the added per-source volume.
    "researcher.plan_inquiry": Tier.WORKHORSE,
    "researcher.extract_evidence": Tier.WORKHORSE,
    "researcher.synthesize": Tier.WORKHORSE,
    "researcher.gap_check": Tier.WORKHORSE,
    "researcher.interpret_experiment": Tier.WORKHORSE,
    "researcher.parse_pricing": Tier.CODE,
    # Evaluation v2 loop (AUDITOR_LOOP=v2). cross_check is per-finding and
    # fans out wide, so it lands on the same WORKHORSE tier as slop_score
    # to inherit the DeepSeek-led premium chain (calibration + groundedness
    # judgments need the reliable model). batch_score is pure aggregation
    # over compact structured input — FAST is enough.
    "evaluation.cross_check_finding": Tier.WORKHORSE,
    "evaluation.batch_score": Tier.FAST,
    # Critic v2 loop (ADVERSARY_LOOP=v2). judge_verdict inherits the
    # legacy REASONING tier — the final kill/weaken decision is the
    # highest-stakes call in the loop. plan_attack is strategy + needs
    # reliable JSON, WORKHORSE. extract_counter is per-page like the
    # researcher's extract_evidence — CODE (local-first qwen for
    # calibration). stress_test_interp is a small synthesis step,
    # WORKHORSE.
    "adversary.plan_attack": Tier.WORKHORSE,
    "adversary.extract_counter": Tier.CODE,
    "adversary.stress_test_interp": Tier.WORKHORSE,
    "adversary.judge_verdict": Tier.REASONING,
    # Planner v2 loop (PLANNER_LOOP=v2). All three steps share the existing
    # planner.generate_tasks tier (WORKHORSE) — task generation is the
    # company's most blast-radius-heavy decision and benefits from the
    # premium chain throughout (planning, proposing, AND self-critique).
    "planner.assess_state": Tier.WORKHORSE,
    "planner.propose_tasks": Tier.WORKHORSE,
    "planner.critique": Tier.WORKHORSE,
}


DAILY_CAPS: dict[Tier, int] = {
    Tier.REASONING: 50,
    # Bumped 800 → 4000 (2026-05-28), then 4000 → 12000 (2026-06-10) when
    # researcher.extract_evidence moved here from CODE: it's the highest-volume
    # researcher call (one per source) and is now cloud so a pool of researchers
    # parallelizes instead of queueing on the local GPU lock. Three pressures:
    # (1) every researcher loop hits WORKHORSE for plan/synthesize/gap/interpret
    # AND now extract (per-source), (2) up to 4 researchers run in parallel,
    # (3) the v2 reworks add WORKHORSE calls (adversary, evaluation cross_check,
    # planner assess+propose+critique). DeepSeek is ~$0.0006/call, so 12000 caps
    # spend at ~$7.20/day worst case. The cap is a safety ceiling; if hit, calls
    # degrade to local qwen3:14b (slower) rather than failing.
    Tier.WORKHORSE: 12000,
    Tier.FAST: 2000,
    Tier.CODE: 500,
    # The experiment design/debug loop can iterate several times per experiment;
    # ~2000 DeepSeek calls/day caps spend ~$1.2/day worst case before degrading to local.
    Tier.EXPERIMENT: 2000,
}


# -------------------------------------------------------------------------
# GPU lock — per-model serialization
# -------------------------------------------------------------------------
#
# History: a single global asyncio.Lock used to serialize EVERY local Ollama
# call. That made one researcher loop iteration fully sequential, which was
# the real bottleneck on swarm throughput.
#
# Reality: Ollama already manages its own concurrency for the same model
# (multiple concurrent /api/chat calls against the same `model` are queued
# inside Ollama and served one at a time per loaded copy, with internal batching
# where possible). The thing the lock was actually protecting against was
# VRAM thrashing — rapid swaps between different models that don't fit
# simultaneously.
#
# New scheme: one lock per model name. Same model → run concurrently (Ollama
# handles it). Different models → run concurrently too, because both GPUs
# (5070 Ti 16GB + 2070 SUPER 8GB) can host different small models at once.
# Ollama decides which GPU loads which based on its scheduler.
#
# For per-call protection against runaway VRAM use, we keep a `max_in_flight`
# global semaphore as a hard cap — far higher than 1, but not infinite.
# -------------------------------------------------------------------------


class GPULock:
    """
    Per-model lock with a global in-flight cap. The `_per_model_locks` map is
    populated lazily — the first call to a given model name gets a fresh lock
    and subsequent calls to the SAME model serialize behind it (since one
    Ollama copy serves them sequentially anyway). Calls to DIFFERENT models
    run concurrently, bounded only by the global semaphore.
    """

    def __init__(self, max_in_flight: int = 4):
        # Reasonable default for a 16GB+8GB host: ~4 small (7-8B Q4) models can
        # be in-flight together. Override via constructor in main.py if needed.
        self._global = asyncio.Semaphore(max_in_flight)
        self._per_model_locks: dict[str, asyncio.Lock] = {}
        self._per_model_lock_lock = asyncio.Lock()
        self._in_flight: dict[str, int] = {}  # for observability / debug
        self._last_active = 0.0  # monotonic time of the last local-GPU call's end (for busy())

    async def _model_lock(self, model_name: str) -> asyncio.Lock:
        # Lazy init under a tiny meta-lock so two concurrent callers don't
        # both create separate Lock objects for the same model.
        async with self._per_model_lock_lock:
            lock = self._per_model_locks.get(model_name)
            if lock is None:
                lock = asyncio.Lock()
                self._per_model_locks[model_name] = lock
        return lock

    @asynccontextmanager
    async def acquire(self, model_name: str):
        await self._global.acquire()
        lock = await self._model_lock(model_name)
        await lock.acquire()
        self._in_flight[model_name] = self._in_flight.get(model_name, 0) + 1
        try:
            yield
        finally:
            self._in_flight[model_name] -= 1
            self._last_active = time.monotonic()
            lock.release()
            self._global.release()

    def busy(self, cooldown_s: float = 0.0) -> bool:
        """True while the lab is using the LOCAL GPU — any call in flight, or within `cooldown_s` of
        the last one finishing (the model is still resident/warm, so its VRAM isn't actually free).
        The Quartermaster reads this so it never launches an experiment into VRAM the lab's own
        retrieval/serving (the priority tenant) needs. Cloud calls don't take this lock, so it's a
        true local-GPU signal."""
        if any(n > 0 for n in self._in_flight.values()):
            return True
        return cooldown_s > 0 and self._last_active > 0 and (time.monotonic() - self._last_active) < cooldown_s


_SHARED_GPU_LOCK: GPULock | None = None


def shared_gpu_lock() -> GPULock:
    """The one process-wide GPULock, so the Router, the experiment LLM broker, graph extraction, and
    the Quartermaster all SEE and SERIALIZE against the same local-GPU activity. (Created on first use;
    main.py grabs it at startup and hands it to the Router + broker.)"""
    global _SHARED_GPU_LOCK
    if _SHARED_GPU_LOCK is None:
        _SHARED_GPU_LOCK = GPULock()
    return _SHARED_GPU_LOCK


# -------------------------------------------------------------------------
# Router
# -------------------------------------------------------------------------


class CostCapExceeded(Exception):
    """Raised when a tier's daily cap is reached and no downgrade is possible."""


class Router:
    """
    Single entry point for all model invocations.

    Flow:
      1. Lookup tier from ROUTE.
      2. Check daily cap; if reasoning capped, downgrade to workhorse with
         a chain-of-thought scaffold appended to the prompt. Other tiers
         halt rather than upgrade.
      3. Open the GPU lock.
      4. Call Ollama with structured output (format=json_schema).
      5. Validate output against the Pydantic schema.
      6. Persist agent_runs row + cost_tracking row + lesson_applications rows.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        gpu_lock: GPULock,
        ollama_url: str = "http://localhost:11434",
        langfuse_client=None,
        call_timeout_s: float = 300.0,
        cloud_chain: list[CloudProvider] | None = None,
        premium_chain: list[CloudProvider] | None = None,
    ):
        self.pool = pool
        self.gpu_lock = gpu_lock
        self.ollama_url = ollama_url
        self.langfuse = langfuse_client if langfuse_client is not None else _maybe_langfuse()
        # Free cloud providers (all tiers); premium leads (PREMIUM_TIERS only).
        self.cloud_chain = cloud_chain or []
        self.premium_chain = premium_chain or []
        # Connection lookup by provider. Same-provider entries (e.g. GitHub
        # mini vs full) share base_url/key, so a single cfg per provider is
        # correct — the model name lives on the per-call ModelSpec.
        self._provider_cfg = {cp.provider: cp for cp in (self.cloud_chain + self.premium_chain)}
        self.cloud_enabled = bool(self.cloud_chain or self.premium_chain)
        # Hard ceiling on a single model call. The GPU lock serializes calls,
        # so one hung Ollama request would otherwise hold the lock and wedge
        # the entire loop. wait_for cancels the call, releases the lock, and
        # the run is marked failed — the loop keeps moving. Slightly above the
        # httpx total timeout so the transport usually errors first with a
        # clearer message; this is the backstop for a hang httpx doesn't catch.
        self.call_timeout_s = call_timeout_s
        self._http = httpx.AsyncClient(timeout=240.0)

    async def close(self):
        await self._http.aclose()

    async def invoke(
        self,
        prompt: BuiltPrompt,
        output_schema_class: type[BaseModel],
        triggered_by_event_id: int | None = None,
        *,
        session: Session | None = None,
        step_name: str | None = None,
        parent_step_id: int | None = None,
    ) -> tuple[BaseModel, int]:
        """
        Run an invocation. Returns (parsed_output, agent_run_id).
        Raises CostCapExceeded when the tier is capped and no downgrade applies.

        When `session` is provided, the run is linked into the session's step
        chain: session_id / step_name / parent_step_id / step_order are
        persisted onto the agent_runs row, and step.started / step.completed
        / step.failed events are emitted tagged with the session_id so the
        trace UI can stream a live DAG.
        """
        invocation_type = prompt.invocation_type
        tier = ROUTE.get(invocation_type)
        if tier is None:
            raise ValueError(f"No route for invocation_type={invocation_type!r}")

        downgraded = False
        local_only = False
        async with self.pool.acquire() as conn:
            used_today = await self._calls_today(conn, tier)
            if used_today >= DAILY_CAPS[tier]:
                fallback = self._downgrade(tier)
                if fallback is not None:
                    tier = fallback
                    downgraded = True
                else:
                    # Capped with no tier downgrade. The cap protects *spend*,
                    # and local Ollama is free — so degrade to local-only
                    # instead of halting. Keeps the loop alive (slowly, for
                    # free) rather than flatlining once the cloud budget is
                    # spent; cloud is retried again after the daily reset.
                    local_only = True
                # Surface the cap hit. Insert once per (tier, day): the events
                # table's UNIQUE (event_type, target_type, target_id, dedup_key)
                # turns subsequent same-day inserts into no-ops. Also flip the
                # cost_tracking.cap_reached flag so /debug's cost panel renders
                # a constrained-budget indicator.
                await self._emit_cap_hit(conn, tier, used_today)

        # Ordered candidates: cloud first (if enabled), local as fallback.
        # When capped, drop the (paid/rate-limited) cloud chain and run local.
        specs = self._call_order(tier)
        if local_only:
            specs = [s for s in specs if s.provider == Provider.OLLAMA]
        primary = specs[0]
        system_text = prompt.as_system_message()
        if downgraded and tier == Tier.WORKHORSE:
            system_text += (
                "\n\n## Reasoning scaffold (you were downgraded — think step-by-step)\n"
                "Before answering, internally list: (1) options considered, "
                "(2) evidence for each, (3) the strongest counter-argument, "
                "(4) your final pick. Then emit the JSON."
            )

        agent_name = invocation_type.split(".")[0]

        # Langfuse v4 span (graceful no-op if not configured / SDK errors)
        lf_span = None
        trace_id = None
        if self.langfuse:
            try:
                lf_span = self.langfuse.start_observation(
                    name=invocation_type,
                    as_type="generation",
                    model=primary.model_name,
                    model_parameters={"temperature": primary.temperature},
                    input={
                        "layers": [layer.name for layer in prompt.layers],
                        "input_tokens": prompt.total_tokens,
                        "tier": tier.value,
                        "downgraded": downgraded,
                    },
                    metadata={"lesson_ids": prompt.lesson_ids},
                )
                trace_id = lf_span.trace_id
            except Exception as e:
                log.warning("Langfuse start failed: %s", e)
                lf_span = None

        # Session linkage: pre-compute step_order and resolve parent_step_id
        # default before the INSERT so the row carries the chain on creation.
        sess_id: int | None = None
        sess_step_order: int | None = None
        sess_parent: int | None = None
        if session is not None and session.id:
            sess_id = session.id
            sess_step_order = session.next_step_order()
            sess_parent = parent_step_id if parent_step_id is not None else session.last_step_id

        async with self.pool.acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO agent_runs (
                    department, agent_name, invocation_type,
                    model_tier, model_name, triggered_by_event_id,
                    input_token_count, input_summary,
                    status, langfuse_trace_id,
                    session_id, step_name, parent_step_id, step_order
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'running', $9,
                        $10, $11, $12, $13)
                RETURNING id
                """,
                agent_name,
                agent_name,
                invocation_type,
                tier.value,
                primary.model_name,
                triggered_by_event_id,
                prompt.total_tokens,
                # Persist the full assembled prompt body so the Debug
                # research-tree view can show exactly what the model saw.
                system_text,
                trace_id,
                sess_id,
                step_name,
                sess_parent,
                sess_step_order,
            )

        if session is not None and session.id:
            # Linear-chain default: next step's parent is this one. Callers
            # passing an explicit parent_step_id (fan-out) won't rely on this.
            session.last_step_id = run_id
            await session.emit_event(
                event_type="step.started",
                payload={
                    "step_name": step_name,
                    "invocation_type": invocation_type,
                    "tier": tier.value,
                    "model": primary.model_name,
                    "step_order": sess_step_order,
                    "parent_step_id": sess_parent,
                },
                emitted_by_run_id=run_id,
            )

        try:
            parsed, output_text, out_tokens, used, fallback_attempts = await self._invoke_with_fallback(
                specs,
                system_text,
                output_schema_class,
            )

            if lf_span:
                try:
                    lf_span.update(
                        output=output_text[:4000],
                        usage_details={"input": prompt.total_tokens, "output": out_tokens},
                    )
                    lf_span.end()
                except Exception:
                    pass

            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                        UPDATE agent_runs
                        SET completed_at = NOW(),
                            status = 'completed',
                            model_name = $1,
                            output_token_count = $2,
                            output_summary = $3,
                            fallback_attempts = $4::jsonb
                        WHERE id = $5
                        """,
                    used.model_name,  # the model that actually produced output
                    out_tokens,
                    # Persist the model's raw JSON output (parsed back to
                    # canonical form) so the tree view can show what came
                    # back. No truncation — agent_runs.output_summary is TEXT.
                    parsed.model_dump_json(indent=2),
                    json.dumps(fallback_attempts),
                    run_id,
                )
                # Only cloud calls count against the daily cap — local is
                # free, so free local work never exhausts the budget (and
                # can't get itself blocked). Replay sessions skip cost
                # tracking entirely so re-running a past step from /trace
                # doesn't double-charge the cap.
                is_replay = session is not None and session.mode == "replay"
                if used.provider != Provider.OLLAMA and not is_replay:
                    await self._record_cost(conn, tier, prompt.total_tokens, out_tokens)
                for lid in prompt.lesson_ids:
                    await conn.execute(
                        "INSERT INTO lesson_applications (lesson_id, agent_run_id) VALUES ($1, $2)",
                        lid,
                        run_id,
                    )

            if session is not None and session.id:
                await session.emit_event(
                    event_type="step.completed",
                    payload={
                        "step_name": step_name,
                        "model": used.model_name,
                        "output_tokens": out_tokens,
                        "fallback_count": len(fallback_attempts),
                    },
                    emitted_by_run_id=run_id,
                )

            return parsed, run_id

        except Exception as e:
            if lf_span:
                try:
                    lf_span.update(output=f"ERROR: {e}", level="ERROR")
                    lf_span.end()
                except Exception:
                    pass
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_runs SET status = 'failed', completed_at = NOW(), error = $1 WHERE id = $2",
                    str(e),
                    run_id,
                )
            if session is not None and session.id:
                await session.emit_event(
                    event_type="step.failed",
                    payload={"step_name": step_name, "error": str(e)[:200]},
                    emitted_by_run_id=run_id,
                )
            raise

    # -- Provider dispatch + fallback ------------------------------------

    def _call_order(self, tier: Tier) -> list[ModelSpec]:
        """Premium leads (for PREMIUM_TIERS) → free cloud chain → local.

        Exception: `Tier.CODE` is **local-first** because we deliberately
        chose qwen2.5-coder:7b for its calibration + JSON discipline on
        per-page extraction (validated by the bench). Falling back to a
        verbose 70B cloud model defeats the purpose of the swap — it makes
        evidence quality random based on whether the cloud chain is 429'd.
        Cloud chain still trails as a fallback for the rare cases qwen errors.

        A provider can legitimately appear twice (GitHub full gpt-4o in the
        premium lead, gpt-4o-mini in the free chain) — different models, same
        connection — so we don't dedupe by provider.
        """
        local = MODELS[tier]
        cloud_chain = self.cloud_chain
        cloud_specs = [
            ModelSpec(
                tier=tier,
                model_name=cp.model_name,
                context_limit=max(local.context_limit, 128_000),
                temperature=local.temperature,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                provider=cp.provider,
            )
            for cp in cloud_chain
        ]

        if tier == Tier.CODE:
            # Local first; cloud trails as fallback only.
            return [local] + cloud_specs

        # Default: premium leads (if applicable) → free cloud chain → local.
        premium_specs = [
            ModelSpec(
                tier=tier,
                model_name=cp.model_name,
                context_limit=max(local.context_limit, 128_000),
                temperature=local.temperature,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                provider=cp.provider,
            )
            for cp in (self.premium_chain if tier in PREMIUM_TIERS else [])
        ]
        return premium_specs + cloud_specs + [local]

    async def _invoke_with_fallback(
        self,
        specs: list[ModelSpec],
        system: str,
        schema_cls: type[BaseModel],
    ) -> tuple[BaseModel, str, int, ModelSpec, list[dict]]:
        """
        Try each candidate in order until one returns schema-valid JSON.
        A rate-limit, timeout, transport error, OR unparseable output all
        trigger the next candidate (so a flaky cloud model degrades to local
        rather than failing the run). Raises the last error if all fail.

        Returns (parsed, raw_text, output_tokens, winning_spec, attempts) where
        `attempts` is the list of per-provider failures that fired before the
        winner — one dict each with {provider, model, error, latency_ms}.
        Persisted on agent_runs.fallback_attempts so /trace can show the full
        chain that fired, not just the winner.
        """
        last_err: Exception | None = None
        attempts: list[dict] = []
        for i, spec in enumerate(specs):
            t0 = time.monotonic()
            try:
                if spec.provider == Provider.OLLAMA:
                    # Local calls serialize on the single GPU.
                    async with self.gpu_lock.acquire(spec.model_name):
                        text, toks = await asyncio.wait_for(
                            self._call_ollama(spec, system, schema_cls),
                            timeout=self.call_timeout_s,
                        )
                else:
                    # Cloud calls are remote — no GPU lock; they can overlap.
                    text, toks = await asyncio.wait_for(
                        self._call_openai_compatible(spec, system, schema_cls),
                        timeout=self.call_timeout_s,
                    )
                parsed = schema_cls.model_validate_json(text)
                return parsed, text, toks, spec, attempts
            except Exception as e:  # noqa: BLE001 — any failure → next candidate
                last_err = e
                attempts.append(
                    {
                        "provider": spec.provider.value,
                        "model": spec.model_name,
                        "error": str(e)[:300],
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                    }
                )
                if i + 1 < len(specs):
                    log.warning(
                        "model %s (%s) failed (%s); falling back to %s",
                        spec.model_name,
                        spec.provider.value,
                        e,
                        specs[i + 1].model_name,
                    )
        assert last_err is not None
        raise last_err

    async def run_single(
        self,
        prompt: BuiltPrompt,
        schema_cls: type[BaseModel],
        provider: Provider,
        model_name: str,
        *,
        temperature: float = 0.3,
        timeout_s: float | None = None,
    ) -> tuple[str, int]:
        """Run exactly one model for `prompt` — no cap check, no fallback, no
        DB/cost accounting. Used by the comparison bench so the caller controls
        exactly which model answers. Returns (raw_text, output_tokens).

        `provider` must be one the Router was constructed with (present in the
        cloud/premium chains) so its base_url/key are known; OLLAMA is always
        available locally.
        """
        spec = ModelSpec(
            tier=Tier.WORKHORSE,  # only affects accounting, which we skip here
            model_name=model_name,
            context_limit=128_000,
            temperature=temperature,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
            provider=provider,
        )
        system = prompt.as_system_message()
        to = timeout_s or self.call_timeout_s
        if provider == Provider.OLLAMA:
            async with self.gpu_lock.acquire(model_name):
                return await asyncio.wait_for(
                    self._call_ollama(spec, system, schema_cls),
                    timeout=to,
                )
        return await asyncio.wait_for(
            self._call_openai_compatible(spec, system, schema_cls),
            timeout=to,
        )

    # -- Cloud call (OpenAI-compatible: Gemini / Groq / NVIDIA / GitHub) --

    async def _call_openai_compatible(
        self,
        model: ModelSpec,
        system: str,
        schema: type[BaseModel],
    ) -> tuple[str, int]:
        cp = self._provider_cfg[model.provider]
        if cp.structured_mode == "json_object":
            # Schema lives in the prompt; we enforce "json" is mentioned so
            # providers that require it (Groq) accept the request.
            response_format = {"type": "json_object"}
            system = (
                system
                + "\n\nReturn a single JSON object that conforms to this schema:\n"
                + json.dumps(schema.model_json_schema())
            )
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        payload = {
            "model": model.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Respond now with the JSON object."},
            ],
            "response_format": response_format,
        }
        if cp.send_temperature:
            payload["temperature"] = model.temperature
        resp = await self._http.post(
            f"{cp.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cp.api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        out_tokens = (data.get("usage") or {}).get("completion_tokens", 0)
        return content, out_tokens

    # -- Ollama call ------------------------------------------------------

    async def _call_ollama(
        self,
        model: ModelSpec,
        system: str,
        schema: type[BaseModel],
    ) -> tuple[str, int]:
        payload = {
            "model": model.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Respond now with the JSON object."},
            ],
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {"temperature": model.temperature},
        }
        resp = await self._http.post(f"{self.ollama_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        out_tokens = data.get("eval_count", 0)
        return content, out_tokens

    # -- Cap tracking ----------------------------------------------------

    async def _calls_today(self, conn, tier: Tier) -> int:
        col = f"{tier.value}_calls"
        result = await conn.fetchval(f"SELECT {col} FROM cost_tracking WHERE day = CURRENT_DATE")
        return result or 0

    async def _emit_cap_hit(self, conn, tier: Tier, used_today: int) -> None:
        """Emit a `cost.cap_reached` event the first time a tier hits its cap
        each day, and flip cost_tracking.cap_reached for the day.

        Dedup is enforced by the events table's UNIQUE constraint on
        (event_type, target_type, target_id, dedup_key) — subsequent calls
        the same day are no-ops, so this is safe to call on every invoke
        while the tier is capped.

        The event surfaces in the /events page and streams via WebSocket
        so a cap hit is visible immediately, not buried in logs.
        """
        from datetime import date as _date

        today = _date.today().isoformat()
        try:
            await conn.execute(
                """
                INSERT INTO events (
                    event_type, target_type, target_id, payload, dedup_key
                )
                VALUES ('cost.cap_reached', 'tier', 0, $1::jsonb, $2)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
                """,
                json.dumps(
                    {
                        "tier": tier.value,
                        "calls_today": used_today,
                        "cap": DAILY_CAPS[tier],
                        "day": today,
                    }
                ),
                f"cap-{tier.value}-{today}",
            )
            # Flag is global (per-day, not per-tier); set true on first cap of any tier.
            await conn.execute(
                """
                INSERT INTO cost_tracking (day, cap_reached)
                VALUES (CURRENT_DATE, TRUE)
                ON CONFLICT (day) DO UPDATE SET cap_reached = TRUE
                """,
            )
        except Exception as e:  # noqa: BLE001 — telemetry must never block the call
            log.warning("failed to emit cost.cap_reached for %s: %s", tier.value, e)

    async def _record_cost(self, conn, tier: Tier, in_toks: int, out_toks: int):
        col = f"{tier.value}_calls"
        spec = MODELS[tier]
        cost = (in_toks / 1000) * spec.cost_per_1k_input + (out_toks / 1000) * spec.cost_per_1k_output
        await conn.execute(
            f"""
            INSERT INTO cost_tracking (day, {col}, total_cost_usd)
            VALUES (CURRENT_DATE, 1, $1)
            ON CONFLICT (day) DO UPDATE
            SET {col} = cost_tracking.{col} + 1,
                total_cost_usd = cost_tracking.total_cost_usd + $1
            """,
            cost,
        )

    def _downgrade(self, tier: Tier) -> Tier | None:
        # Only reasoning downgrades. Other tiers capped = halt.
        return Tier.WORKHORSE if tier == Tier.REASONING else None

    def _summarize(self, parsed: BaseModel) -> str:
        s = parsed.model_dump_json()
        return s[:500] + "…" if len(s) > 500 else s
