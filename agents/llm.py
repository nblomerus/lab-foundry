"""
Shared LLM path for the lab's agents — DeepSeek (cloud) → local Ollama, per the
"only DeepSeek or local" policy. Extracted from agents.ariadne.loop so Ariadne, the
Planner, and Mimir can all use it without import cycles.

`_chain_complete` runs a chat completion across the provider chain (DeepSeek primary with
full retries; on a SUSTAINED failure it fails over to free local inference), and records the
call to /trace as an agent_run (agent derived from invocation_type, e.g. 'mimir.ask' -> mimir).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re

import httpx

from harness.dispatch import _current_session
from harness.router import CloudProvider, Provider

log = logging.getLogger(__name__)

LLM_RETRIES = int(os.environ.get("ARIADNE_LLM_RETRIES", "4"))
# Local fallback: qwen3:14b (fast, JSON-disciplined). The 32b-r1 distill is far more capable
# but impractically slow here (>5min), so it's not the default resort.
LOCAL_MODEL = os.environ.get("ARIADNE_LOCAL_MODEL", "qwen3:14b")
_RETRYABLE_TRANSPORT = (httpx.TimeoutException, httpx.TransportError)


def _llm_chain() -> list[CloudProvider]:
    """The model chain — DeepSeek (cloud) → local Ollama. DeepSeek primary, local always-up
    fallback so a cloud outage degrades to free local inference instead of crashing."""
    env = os.environ
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
    ollama = (env.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
    chain.append(
        CloudProvider(Provider.OLLAMA, f"{ollama}/v1", env.get("OLLAMA_API_KEY", "ollama"), LOCAL_MODEL, "json_object")
    )
    return chain


async def _llm_post(client: httpx.AsyncClient, url: str, *, retries: int = LLM_RETRIES, **kwargs) -> httpx.Response:
    """POST with bounded exponential backoff on transient faults (5xx / timeout / network).
    4xx raises immediately — a bad request won't fix itself by retrying."""
    delay, last_exc = 2.0, None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(url, **kwargs)
            if resp.status_code < 500:
                resp.raise_for_status()  # 2xx returns; 4xx raises (non-retryable)
                return resp
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code} {resp.reason_phrase}", request=resp.request, response=resp
            )
        except _RETRYABLE_TRANSPORT as e:
            last_exc = e
        if attempt < retries:
            log.warning("LLM transient fault (attempt %d/%d): %s — retrying in %.0fs", attempt, retries, last_exc, delay)
            await asyncio.sleep(delay)
            delay *= 2
    assert last_exc is not None
    raise last_exc


async def _record_run(
    model_name: str,
    in_tok,
    out_tok,
    *,
    invocation_type: str,
    step_name: str,
    input_summary: str | None = None,
    output_summary: str | None = None,
) -> None:
    """Best-effort: log this LLM call as an agent_run linked to the current handler session so
    it shows in /trace. Persists the full prompt (input_summary) and completion (output_summary)
    — TEXT, no truncation, matching harness.router — so /trace shows exactly what the model saw
    and produced (e.g. the `ask` step IS Ariadne's question to Mimir + Mimir's answer). NEVER
    raises — observability must not break the call."""
    sess = _current_session.get()
    pool = getattr(sess, "_pool", None)
    if sess is None or not getattr(sess, "id", 0) or pool is None:
        return
    agent = invocation_type.split(".")[0]
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_runs (department, agent_name, invocation_type, model_tier, model_name, "
                "triggered_by_event_id, input_token_count, output_token_count, status, completed_at, "
                "session_id, step_name, step_order, input_summary, output_summary) "
                "VALUES ('research',$1,$2,'reasoning',$3,$4,$5,$6,'completed',now(),$7,$8,$9,$10,$11)",
                agent,
                invocation_type,
                model_name,
                sess.triggered_by_event_id,
                in_tok,
                out_tok,
                sess.id,
                step_name,
                sess.next_step_order(),
                input_summary,
                output_summary,
            )
    except Exception as e:  # noqa: BLE001 — observability is best-effort
        log.debug("agent_run record skipped (%s): %s", step_name, e)


async def _chain_complete(
    messages: list[dict], *, temperature: float, invocation_type: str, step_name: str, primary_model: str | None = None
) -> str:
    """Run a chat completion across the provider chain (DeepSeek → local). DeepSeek primary with
    full retries; on a SUSTAINED failure, fail over to local. Records the call to /trace."""
    chain = _llm_chain()
    if not chain:
        raise RuntimeError("no LLM provider configured (need DEEPSEEK_API_KEY or a local Ollama)")
    if primary_model and chain[0].provider == Provider.DEEPSEEK:
        chain[0] = dataclasses.replace(chain[0], model_name=primary_model)
    # The full prompt the model sees (every role), persisted to /trace as the step's input.
    input_summary = "\n\n".join(f"## {m.get('role', '?')}\n{m.get('content', '')}" for m in messages)

    last_exc = None
    async with httpx.AsyncClient(timeout=180.0) as client:
        for i, cp in enumerate(chain):
            try:
                resp = await _llm_post(
                    client,
                    f"{cp.base_url.rstrip('/')}/chat/completions",
                    retries=LLM_RETRIES if i == 0 else 2,
                    headers={"Authorization": f"Bearer {cp.api_key}"},
                    json={
                        "model": cp.model_name,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": temperature,
                    },
                )
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                usage = data.get("usage") or {}
                if i > 0:
                    log.warning("%s: failed over to %s (%s) after primary", step_name, cp.provider.value, cp.model_name)
                await _record_run(
                    cp.model_name,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    invocation_type=invocation_type,
                    step_name=step_name,
                    input_summary=input_summary,
                    output_summary=content,
                )
                return content
            except Exception as e:  # noqa: BLE001 — exhausted this provider; try the next
                last_exc = e
                log.warning(
                    "%s: provider %s (%s) failed: %s — trying next", step_name, cp.provider.value, cp.model_name, e
                )
    raise last_exc if last_exc else RuntimeError("all providers failed")


def _strip_fences(content: str) -> str:
    # DeepSeek-R1 / local reasoning models emit a <think>…</think> preamble before the JSON.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    return content
