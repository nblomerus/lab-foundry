"""
Agent Lab — run a single agent in isolation.

Two run semantics, by agent kind:
  * LLM agents (PI, Planner, Researcher, Critic, Evaluation, Reflection) run DRY:
    assemble the agent's real context (reuse api.bench.build_context), build the
    real prompt, run the agent's tier model once via router.run_single (NO caps,
    NO DB writes, NO events — nothing downstream wakes), and return the validated
    structured output. This is "what would this agent decide, right now."
  * Mimir runs SAFE-LIVE: its paths are deterministic + idempotent, so seed /
    sweep / acquire actually execute against a dedicated pgvector pool and return
    the real trust verdict / discovered sources / acquire reply.

Endpoints:
    GET  /agentlab/agents   — catalog (agents → modes → input fields) + claims
    POST /agentlab/run      — { agent, mode, claim_id?, inputs{} } → result
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time

import asyncpg
import pgvector.asyncpg
from fastapi import APIRouter, Request
from pydantic import BaseModel

from agents.mimir.acquire import AcquireRequest, handle_acquire_requested
from agents.mimir.collectors import run_discovery_sweep
from agents.mimir.handler import ingest_source
from api.bench import BenchContextError, build_context, get_engine
from harness.curator import RECIPES
from harness.router import MODELS, PREMIUM_TIERS, ROUTE, Tier
from state.client import PostgresClient

log = logging.getLogger("api.agentlab")
router = APIRouter(prefix="/agentlab", tags=["agentlab"])

LLM = "llm"
MIMIR = "mimir"

# What each agent emits live (shown as "would emit" on a dry run).
_EMITS: dict[str, str] = {
    "pi.exploration_kickoff": "company.bootstrapped + claims",
    "pi.claim_verdict": "claim.invalidated (on kill)",
    "pi.spawn_claim": "claim.created (on spawn)",
    "pi.phase_transition_ratify": "phase transition + charter",
    "planner.generate_tasks": "task.created ×N",
    "researcher.execute_task": "findings + task.completed",
    "critic.kill_verdict": "critic verdict → claim.invalidated/confidence_changed",
    "evaluation.slop_score": "finding.high_signal / audit.slop_detected",
    "reflect.lesson_propose": "lesson candidate",
}

def _llm(key: str, label: str, invocation_type: str, needs_claim: bool = False) -> dict:
    m: dict = {"key": key, "label": label, "kind": LLM, "invocation_type": invocation_type, "inputs": []}
    if needs_claim:
        m["needs_claim"] = True
    return m


_MIMIR_MODES = [
    {
        "key": "seed", "label": "Seed a source", "kind": MIMIR, "action": "seed",
        "inputs": [
            {"name": "arxiv_id", "label": "arXiv ID", "placeholder": "1706.03762"},
            {"name": "url", "label": "or URL", "placeholder": "https://example.com/post"},
        ],
        "note": "Ingests for real (idempotent). arXiv → preprint; a plain URL can "
                "trigger the web_unknown LLM tie-breaker.",
    },
    {
        "key": "sweep", "label": "Discovery sweep", "kind": MIMIR, "action": "sweep",
        "inputs": [{"name": "topic", "label": "Topic", "placeholder": "mixture of experts routing"}],
        "note": "Runs the scouts over the topic and emits source.discovered per new source.",
    },
    {
        "key": "acquire", "label": "Acquire (pull)", "kind": MIMIR, "action": "acquire",
        "inputs": [
            {"name": "query", "label": "Query", "placeholder": "speculative decoding"},
            {"name": "arxiv_id", "label": "or arXiv ID", "placeholder": "2211.17192"},
        ],
        "note": "A researcher-style pull: resolve → dedupe → trust-gated ingest → reply.",
    },
]

# The catalog IS the run registry — the run endpoint looks modes up here, so the
# client can't inject arbitrary invocation_types.
AGENTS: list[dict] = [
    {
        "id": "mimir", "label": "Mimir", "role": "Warden of the Library", "status": "live",
        "what": "Ingests a source through the trust gate (deterministic ladder + an LLM "
                "tie-breaker), runs a discovery sweep, or fulfils an acquire request.",
        "modes": _MIMIR_MODES,
    },
    {
        "id": "pi", "label": "Ariadne (PI)", "role": "Principal investigator", "status": "planned",
        "what": "Frames research directions, ratifies kills, spawns replacements, and ratifies phase transitions.",
        "modes": [
            _llm("exploration_kickoff", "Exploration kickoff", "pi.exploration_kickoff"),
            _llm("claim_verdict", "Claim kill verdict", "pi.claim_verdict", needs_claim=True),
            _llm("spawn_claim", "Spawn replacement", "pi.spawn_claim"),
            _llm("phase_ratify", "Phase transition ratify", "pi.phase_transition_ratify"),
        ],
    },
    {
        "id": "planner", "label": "Planner", "role": "Task planning", "status": "planned",
        "what": "Turns active claims into concrete, falsifiable research tasks when the queue drains.",
        "modes": [_llm("generate_tasks", "Generate tasks", "planner.generate_tasks")],
    },
    {
        "id": "researcher", "label": "Researchers", "role": "Investigate & gather", "status": "planned",
        "what": "Investigates a task/claim, gathers evidence, and produces findings.",
        "modes": [_llm("execute_task", "Execute task", "researcher.execute_task", needs_claim=True)],
    },
    {
        "id": "critic", "label": "Critic", "role": "Challenge claims", "status": "planned",
        "what": "Hunts contradictions and decides watch / weaken / kill on a claim.",
        "modes": [_llm("kill_verdict", "Kill verdict", "critic.kill_verdict", needs_claim=True)],
    },
    {
        "id": "evaluation", "label": "Evaluation", "role": "Audit & score", "status": "planned",
        "what": "Scores a task's findings for substance and groundedness against their evidence trail.",
        "modes": [_llm("slop_score", "Slop score", "evaluation.slop_score")],
    },
    {
        "id": "reflection", "label": "Reflection", "role": "Learn lessons", "status": "planned",
        "what": "Judges whether a past run yields a generalizable lesson worth keeping.",
        "modes": [_llm("lesson_propose", "Propose lesson", "reflect.lesson_propose")],
    },
]

_MODE_INDEX = {(a["id"], m["key"]): (a, m) for a in AGENTS for m in a["modes"]}


def _resolve_model(invocation_type: str, eng) -> tuple[object, str, str]:
    """The model the live system would use for this recipe's tier: the premium
    chain lead for premium tiers, else the local fallback. Returns
    (Provider, model_name, tier_name)."""
    tier = ROUTE.get(invocation_type, Tier.WORKHORSE)
    if tier in PREMIUM_TIERS and eng.router.premium_chain:
        cp = eng.router.premium_chain[0]
        return cp.provider, cp.model_name, tier.value
    spec = MODELS[tier]
    return spec.provider, spec.model_name, tier.value


# -------------------------------------------------------------------------
# Mimir needs a pgvector-enabled pool (embed writes bind vector(768)); the API's
# default pool only has the JSONB codec. Build one, cached on app.state.
# -------------------------------------------------------------------------
async def _mimir_init(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await pgvector.asyncpg.register_vector(conn)


async def _mimir_state(app) -> PostgresClient:
    st = getattr(app.state, "agentlab_mimir_state", None)
    if st is None:
        pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=1, max_size=4, init=_mimir_init
        )
        st = PostgresClient(pool=pool)
        app.state.agentlab_mimir_state = st
    return st


@router.get("/agents")
async def agents(request: Request) -> dict:
    eng = await get_engine(request.app)
    # annotate each LLM mode with the model it would run + whether its schema resolves
    catalog = []
    for a in AGENTS:
        modes = []
        for m in a["modes"]:
            mm = {k: v for k, v in m.items() if k != "invocation_type"}
            if m["kind"] == LLM:
                inv = m["invocation_type"]
                provider, model_name, tier = _resolve_model(inv, eng)
                recipe = RECIPES.get(inv)
                schema_name = recipe.output_schema if recipe else None
                mm["invocation_type"] = inv
                mm["tier"] = tier
                mm["model"] = f"{getattr(provider, 'value', provider)}:{model_name}"
                mm["output_schema"] = schema_name
                mm["runnable"] = bool(recipe and eng.schemas.get(schema_name))
                mm["emits"] = _EMITS.get(inv)
            modes.append(mm)
        catalog.append({**a, "modes": modes})
    try:
        claims = [{"id": c.id, "claim": c.statement} for c in await eng.state.get_active_claims(limit=25)]
    except Exception:  # noqa: BLE001
        claims = []
    return {"agents": catalog, "claims": claims}


class RunRequest(BaseModel):
    agent: str
    mode: str
    claim_id: int | None = None
    inputs: dict[str, str] = {}


@router.post("/run")
async def run(request: Request, req: RunRequest) -> dict:
    found = _MODE_INDEX.get((req.agent, req.mode))
    if found is None:
        return {"status": "error", "error": f"unknown agent/mode {req.agent}/{req.mode}"}
    _agent, mode = found

    if mode["kind"] == LLM:
        return await _run_llm(request.app, mode, req.claim_id)
    return await _run_mimir(request.app, mode, req.inputs)


async def _run_llm(app, mode: dict, claim_id: int | None) -> dict:
    eng = await get_engine(app)
    inv = mode["invocation_type"]
    recipe = RECIPES.get(inv)
    schema_cls = eng.schemas.get(recipe.output_schema) if recipe else None
    if recipe is None or schema_cls is None:
        return {"status": "error", "error": f"{inv} has no resolvable output schema"}
    try:
        ctx, ctx_note = await build_context(eng, inv, claim_id)
    except BenchContextError as e:
        return {"status": "error", "error": str(e), "kind": LLM, "invocation_type": inv}
    prompt = await eng.curator.build(inv, ctx)
    provider, model_name, tier = _resolve_model(inv, eng)
    t0 = time.perf_counter()
    try:
        text, out_tokens = await eng.router.run_single(prompt, schema_cls, provider, model_name, timeout_s=240.0)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "kind": LLM, "invocation_type": inv,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
                "model": f"{getattr(provider, 'value', provider)}:{model_name}", "tier": tier}
    dt = int((time.perf_counter() - t0) * 1000)
    parsed = None
    with contextlib.suppress(Exception):
        parsed = json.loads(text)
    valid, validated, verr = False, None, None
    try:
        validated = schema_cls.model_validate_json(text).model_dump(mode="json")
        valid = True
    except Exception as e:  # noqa: BLE001
        verr = str(e)[:300]
    return {
        "status": "ok", "kind": LLM, "dry_run": True, "invocation_type": inv, "tier": tier,
        "model": f"{getattr(provider, 'value', provider)}:{model_name}",
        "context_note": ctx_note, "prompt_tokens": prompt.total_tokens,
        "prompt_preview": prompt.as_system_message()[:12000],
        "latency_ms": dt, "output_tokens": out_tokens,
        "parsed": parsed, "raw": None if parsed is not None else text[:4000],
        "valid": valid, "validated": validated, "validation_error": verr,
        "would_emit": _EMITS.get(inv),
    }


async def _run_mimir(app, mode: dict, inputs: dict[str, str]) -> dict:
    eng = await get_engine(app)
    state = await _mimir_state(app)
    action = mode["action"]

    if action == "seed":
        arxiv_id = (inputs.get("arxiv_id") or "").strip()
        url = (inputs.get("url") or "").strip()
        if arxiv_id:
            source = {"kind": "paper", "source_kind": "arxiv", "canonical_key": arxiv_id,
                      "url": f"https://arxiv.org/abs/{arxiv_id}", "arxiv_id": arxiv_id, "why": "agent-lab seed"}
        elif url:
            source = {"kind": "web", "source_kind": "web", "canonical_key": url, "url": url,
                      "why": "agent-lab seed (web — exercises the trust tie-breaker)"}
        else:
            return {"status": "error", "kind": MIMIR, "error": "provide an arXiv ID or a URL"}
        try:
            res = await ingest_source(source, state, router=eng.router, curator=eng.curator, session=None)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "kind": MIMIR, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return {"status": "ok", "kind": MIMIR, "live": True, "action": "seed", "result": res}

    if action == "sweep":
        topic = (inputs.get("topic") or "").strip()
        topics = [topic] if topic else None
        try:
            res = await run_discovery_sweep(topics, state, per_topic=4)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "kind": MIMIR, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return {"status": "ok", "kind": MIMIR, "live": True, "action": "sweep", "result": res,
                "note": "source.discovered emitted per new source; the running harness ingests them."}

    if action == "acquire":
        query = (inputs.get("query") or "").strip()
        arxiv_id = (inputs.get("arxiv_id") or "").strip()
        if not query and not arxiv_id:
            return {"status": "error", "kind": MIMIR, "error": "provide a query or an arXiv ID"}
        why = "agent-lab acquire test: a researcher needs this source to ground a specific claim"
        req = AcquireRequest(requester="researcher", why=why, arxiv_id=arxiv_id or None, query=query or None)

        class _Shim:
            pass

        shim = _Shim()
        shim.state, shim.router, shim.curator, shim.session = state, eng.router, eng.curator, None
        try:
            res = await handle_acquire_requested({"id": 0, "payload": req.model_dump()}, shim)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "kind": MIMIR, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return {"status": "ok", "kind": MIMIR, "live": True, "action": "acquire", "result": res}

    return {"status": "error", "kind": MIMIR, "error": f"unknown mimir action {action}"}
