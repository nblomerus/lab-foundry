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
    GEMINI = "gemini"   # cloud, OpenAI-compatible endpoint, no GPU lock


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

# Cloud model. One frontier-class model serves every tier; it's preferred
# over local when a key is configured, with local as the automatic fallback
# on rate-limit / error / unparseable output. Free tier → $0 cost. Tier-
# specific temperature is reused from the local spec so behaviour matches.
CLOUD_MODEL_NAME = os.environ.get("CLOUD_MODEL", "gemini-2.5-flash")


def cloud_spec_for(tier: Tier) -> ModelSpec:
    local = MODELS[tier]
    return ModelSpec(
        tier=tier,
        model_name=CLOUD_MODEL_NAME,
        context_limit=max(local.context_limit, 128_000),
        temperature=local.temperature,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        provider=Provider.GEMINI,
    )


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
        gemini_api_key: Optional[str] = None,
        gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
    ):
        self.pool = pool
        self.gpu_lock = gpu_lock
        self.ollama_url = ollama_url
        self.langfuse = langfuse_client if langfuse_client is not None else _maybe_langfuse()
        # Cloud provider (Gemini). Enabled only when a key is present;
        # otherwise every tier stays local exactly as before.
        self.gemini_api_key = gemini_api_key
        self.gemini_base_url = gemini_base_url.rstrip("/")
        self.cloud_enabled = bool(gemini_api_key)
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
        """Cloud first when enabled, local Ollama always as the tail fallback."""
        local = MODELS[tier]
        if self.cloud_enabled:
            return [cloud_spec_for(tier), local]
        return [local]

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

    # -- Cloud call (OpenAI-compatible: Gemini) --------------------------

    async def _call_openai_compatible(
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
            "temperature": model.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        resp = await self._http.post(
            f"{self.gemini_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.gemini_api_key}"},
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
