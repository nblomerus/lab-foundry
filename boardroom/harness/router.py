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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Optional, Type

import asyncpg
import httpx
import logging
import os
from pydantic import BaseModel

from boardroom.harness.curator import BuiltPrompt


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
    FAST      = "fast"
    CODE      = "code"


class Provider(enum.Enum):
    OLLAMA = "ollama"   # local, serialized behind the GPU lock
    GEMINI = "gemini"   # Google AI Studio, OpenAI-compatible
    GROQ   = "groq"     # Groq LPU (fast open models), OpenAI-compatible
    NVIDIA = "nvidia"   # NVIDIA NIM catalog, OpenAI-compatible
    GITHUB = "github"   # GitHub Models (GPT-4o, Llama, …), OpenAI-compatible
    OPENAI = "openai"   # OpenAI direct (personal key, paid) — premium tier only


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
        model_name="qwen2.5:14b-instruct-q4_K_M",
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
    """Assemble the ordered cloud chain from whichever keys are present.

    Order = fastest-reliable first. Model per provider is overridable by env
    (GEMINI_MODEL / GROQ_MODEL / NVIDIA_MODEL).
    """
    chain: list[CloudProvider] = []
    if env.get("GEMINI_API_KEY"):
        chain.append(CloudProvider(
            Provider.GEMINI,
            "https://generativelanguage.googleapis.com/v1beta/openai",
            env["GEMINI_API_KEY"],
            env.get("GEMINI_MODEL", "gemini-2.5-flash"),
            "json_schema",
        ))
    # Note: the gsk_ key is Groq (groq.com), not xAI Grok.
    if env.get("GROK_API_KEY") or env.get("GROQ_API_KEY"):
        chain.append(CloudProvider(
            Provider.GROQ,
            "https://api.groq.com/openai/v1",
            env.get("GROQ_API_KEY") or env["GROK_API_KEY"],
            env.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "json_object",
        ))
    gh = env.get("GITHUB_MODELS_TOKEN") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if gh:
        chain.append(CloudProvider(
            Provider.GITHUB,
            env.get("GITHUB_MODELS_URL", "https://models.github.ai/inference"),
            gh,
            env.get("GITHUB_MODEL", "openai/gpt-4o-mini"),
            "json_schema",
        ))
    # NVIDIA last among cloud — verified working with json_schema but slow
    # (~17s), so it's the final cloud resort before local.
    if env.get("NVA_API_KEY") or env.get("NVIDIA_API_KEY"):
        chain.append(CloudProvider(
            Provider.NVIDIA,
            "https://integrate.api.nvidia.com/v1",
            env.get("NVIDIA_API_KEY") or env["NVA_API_KEY"],
            env.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
            "json_schema",
        ))
    return chain


# Tiers important enough to lead with premium models (paid OpenAI / full
# GPT-4o) before the free chain. These are the company's highest-stakes,
# lowest-volume decisions, so the cost is trivial and the quality matters.
PREMIUM_TIERS = {Tier.REASONING}


def build_premium_chain(env: dict) -> list[CloudProvider]:
    """Quality-first leads for PREMIUM_TIERS, tried before the free chain.

    OpenAI direct (personal key) is preferred; GitHub's full gpt-4o is the
    next-best that works on the free tier, so the chain still delivers a
    frontier-class model even before OpenAI billing is set up.
    """
    chain: list[CloudProvider] = []
    if env.get("OPENAI_API_KEY"):
        model = env.get("OPENAI_MODEL", "gpt-5.5")
        # GPT-5.x / o-series reject custom temperature; only classic chat
        # models (gpt-4o, gpt-4.1, …) accept it.
        send_temp = not (model.startswith(("gpt-5", "o1", "o3", "o4")))
        chain.append(CloudProvider(
            Provider.OPENAI,
            "https://api.openai.com/v1",
            env["OPENAI_API_KEY"],
            model,
            "json_schema",
            send_temp,
        ))
    gh = env.get("GITHUB_MODELS_TOKEN") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if gh:
        chain.append(CloudProvider(
            Provider.GITHUB,
            env.get("GITHUB_MODELS_URL", "https://models.github.ai/inference"),
            gh,
            env.get("GITHUB_PREMIUM_MODEL", "openai/gpt-4o"),  # full gpt-4o, not mini
            "json_schema",
        ))
    return chain


# -------------------------------------------------------------------------
# Route table — single source of truth for "what runs this"
# -------------------------------------------------------------------------

ROUTE: dict[str, Tier] = {
    # Reasoning — high-stakes; capped at 4/day
    "ceo.thesis_kill":               Tier.REASONING,
    "ceo.phase_transition_proposal": Tier.REASONING,
    "ceo.charter_write":             Tier.REASONING,
    "adversary.kill_verdict":        Tier.REASONING,

    # Workhorse — standard strategic / tactical
    "ceo.exploration_kickoff":       Tier.WORKHORSE,
    "ceo.weekly_synthesis":          Tier.WORKHORSE,
    "ceo.thesis_rescore":            Tier.WORKHORSE,
    "ceo.spawn_replacement":         Tier.WORKHORSE,
    "planner.generate_tasks":        Tier.WORKHORSE,
    "adversary.contradiction_hunt":  Tier.WORKHORSE,

    # Fast — verifiers and high-volume classifiers
    "auditor.slop_score":            Tier.FAST,
    "auditor.relevance_verify":      Tier.FAST,
    "phase_adjudicator.check":       Tier.FAST,
    "curator.compact_recall":        Tier.FAST,
    "reflect.lesson_propose":        Tier.FAST,

    # Code — tool-using extractors
    "researcher.execute_task":       Tier.CODE,
    "researcher.summarize_source":   Tier.CODE,
}


DAILY_CAPS: dict[Tier, int] = {
    Tier.REASONING: 4,
    Tier.WORKHORSE: 200,
    Tier.FAST:      2000,
    Tier.CODE:      500,
}


# -------------------------------------------------------------------------
# GPU lock — only one Ollama model hot at a time
# -------------------------------------------------------------------------

class GPULock:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._current_model: Optional[str] = None

    @asynccontextmanager
    async def acquire(self, model_name: str):
        async with self._lock:
            self._current_model = model_name
            try:
                yield
            finally:
                self._current_model = None


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
        cloud_chain: Optional[list[CloudProvider]] = None,
        premium_chain: Optional[list[CloudProvider]] = None,
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
        output_schema_class: Type[BaseModel],
        triggered_by_event_id: Optional[int] = None,
    ) -> tuple[BaseModel, int]:
        """
        Run an invocation. Returns (parsed_output, agent_run_id).
        Raises CostCapExceeded when the tier is capped and no downgrade applies.
        """
        invocation_type = prompt.invocation_type
        tier = ROUTE.get(invocation_type)
        if tier is None:
            raise ValueError(f"No route for invocation_type={invocation_type!r}")

        downgraded = False
        async with self.pool.acquire() as conn:
            used_today = await self._calls_today(conn, tier)
            if used_today >= DAILY_CAPS[tier]:
                fallback = self._downgrade(tier)
                if fallback is None:
                    raise CostCapExceeded(invocation_type)
                tier = fallback
                downgraded = True

        # Ordered candidates: cloud first (if enabled), local as fallback.
        specs = self._call_order(tier)
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
                        "layers":       [l.name for l in prompt.layers],
                        "input_tokens": prompt.total_tokens,
                        "tier":         tier.value,
                        "downgraded":   downgraded,
                    },
                    metadata={"lesson_ids": prompt.lesson_ids},
                )
                trace_id = lf_span.trace_id
            except Exception as e:
                log.warning("Langfuse start failed: %s", e)
                lf_span = None

        async with self.pool.acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO agent_runs (
                    department, agent_name, invocation_type,
                    model_tier, model_name, triggered_by_event_id,
                    input_token_count, status, langfuse_trace_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'running', $8)
                RETURNING id
                """,
                agent_name, agent_name, invocation_type,
                tier.value, primary.model_name, triggered_by_event_id,
                prompt.total_tokens, trace_id,
            )

        try:
            parsed, output_text, out_tokens, used = await self._invoke_with_fallback(
                specs, system_text, output_schema_class,
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

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE agent_runs
                        SET completed_at = NOW(),
                            status = 'completed',
                            model_name = $1,
                            output_token_count = $2,
                            output_summary = $3
                        WHERE id = $4
                        """,
                        used.model_name,   # the model that actually produced output
                        out_tokens,
                        self._summarize(parsed),
                        run_id,
                    )
                    await self._record_cost(conn, tier, prompt.total_tokens, out_tokens)
                    for lid in prompt.lesson_ids:
                        await conn.execute(
                            "INSERT INTO lesson_applications (lesson_id, agent_run_id) "
                            "VALUES ($1, $2)",
                            lid, run_id,
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
                    "UPDATE agent_runs SET status = 'failed', completed_at = NOW(), "
                    "error = $1 WHERE id = $2",
                    str(e), run_id,
                )
            raise

    # -- Provider dispatch + fallback ------------------------------------

    def _call_order(self, tier: Tier) -> list[ModelSpec]:
        """Premium leads (for PREMIUM_TIERS) → free cloud chain → local.

        A provider can legitimately appear twice (GitHub full gpt-4o in the
        premium lead, gpt-4o-mini in the free chain) — different models, same
        connection — so we don't dedupe by provider.
        """
        local = MODELS[tier]
        chain = (self.premium_chain if tier in PREMIUM_TIERS else []) + self.cloud_chain
        specs = [
            ModelSpec(
                tier=tier,
                model_name=cp.model_name,
                context_limit=max(local.context_limit, 128_000),
                temperature=local.temperature,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                provider=cp.provider,
            )
            for cp in chain
        ]
        specs.append(local)
        return specs

    async def _invoke_with_fallback(
        self,
        specs: list[ModelSpec],
        system: str,
        schema_cls: Type[BaseModel],
    ) -> tuple[BaseModel, str, int, ModelSpec]:
        """
        Try each candidate in order until one returns schema-valid JSON.
        A rate-limit, timeout, transport error, OR unparseable output all
        trigger the next candidate (so a flaky cloud model degrades to local
        rather than failing the run). Raises the last error if all fail.
        """
        last_err: Optional[Exception] = None
        for i, spec in enumerate(specs):
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
                return parsed, text, toks, spec
            except Exception as e:  # noqa: BLE001 — any failure → next candidate
                last_err = e
                if i + 1 < len(specs):
                    log.warning(
                        "model %s (%s) failed (%s); falling back to %s",
                        spec.model_name, spec.provider.value, e, specs[i + 1].model_name,
                    )
        assert last_err is not None
        raise last_err

    # -- Cloud call (OpenAI-compatible: Gemini / Groq / NVIDIA / GitHub) --

    async def _call_openai_compatible(
        self,
        model: ModelSpec,
        system: str,
        schema: Type[BaseModel],
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
        schema: Type[BaseModel],
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
        result = await conn.fetchval(
            f"SELECT {col} FROM cost_tracking WHERE day = CURRENT_DATE"
        )
        return result or 0

    async def _record_cost(self, conn, tier: Tier, in_toks: int, out_toks: int):
        col = f"{tier.value}_calls"
        spec = MODELS[tier]
        cost = (in_toks / 1000) * spec.cost_per_1k_input + \
               (out_toks / 1000) * spec.cost_per_1k_output
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

    def _downgrade(self, tier: Tier) -> Optional[Tier]:
        # Only reasoning downgrades. Other tiers capped = halt.
        return Tier.WORKHORSE if tier == Tier.REASONING else None

    def _summarize(self, parsed: BaseModel) -> str:
        s = parsed.model_dump_json()
        return s[:500] + "…" if len(s) > 500 else s
