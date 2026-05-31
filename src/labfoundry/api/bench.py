"""
Model comparison bench.

A read-only playground: pick a template task (any invocation_type the harness
runs) and a set of models, and run the *real* prompt that task would use across
all of them side by side. Nothing here touches the live control loop — no cap
accounting, no DB writes, no effect on what the running company uses. It exists
purely to see the quality/latency difference between models on each task.

Endpoints:
    GET  /bench/options   — runnable tasks, available models, theses for context
    POST /bench/run       — build one task's prompt, run it across N models
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

import labfoundry.adversarial.loop  # noqa: F401  (registers critic.plan_attack / extract_counter / …)
import labfoundry.audit.loop  # noqa: F401  (registers evaluation.cross_check_finding / batch_score)

# Importing the handler modules registers their recipes + output schemas as a
# side effect (each does `RECIPES[x] = Recipe(...)` at import time). Without
# this, only the 3 recipes defined in curator.py would be runnable.
import labfoundry.bootstrap  # noqa: F401  (ExplorationKickoffOutput)
import labfoundry.planner.loop  # noqa: F401  (registers planner.assess_state / propose_tasks / critique)
import labfoundry.research.experiments  # noqa: F401  (registers researcher.parse_pricing)

# Side-effect imports: register the agentic-researcher recipes + schemas so
# they're benchable from the /bench tab.
import labfoundry.research.loop  # noqa: F401  (registers researcher.* recipes)
from labfoundry.adversarial import schemas as adversarial_schemas  # noqa: F401
from labfoundry.audit import schemas as audit_schemas  # noqa: F401
from labfoundry.handlers import (  # noqa: F401
    audit_slop_detected,
    claim_invalidated,
    critic,
    phase_adjudicator,
    phase_budget_exceeded,
    phase_transition,
    queue_empty,
    reflection,
    researcher,
    task_completed,
)
from labfoundry.harness.curator import RECIPES, Curator
from labfoundry.harness.router import (
    MODELS,
    ROUTE,
    GPULock,
    Provider,
    Router,
    Tier,
    build_cloud_chain,
    build_premium_chain,
)
from labfoundry.memory.client import ZepClient
from labfoundry.planner import schemas as planner_schemas  # noqa: F401
from labfoundry.research import schemas as research_schemas  # noqa: F401
from labfoundry.skills.client import LessonsClient
from labfoundry.state.client import PostgresClient

log = logging.getLogger("api.bench")
router = APIRouter(prefix="/bench", tags=["bench"])

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Sessions Zep expects (mirror of harness.main.ZEP_SESSIONS).
ZEP_SESSIONS = ["claims-lifecycle", "phase-transitions", "pi-deliberations", "dissent", "charter"]

# Tasks whose prompt is grounded in a specific thesis (so the UI offers a
# thesis picker). Everything else builds from global company state.
THESIS_SCOPED = {
    "critic.kill_verdict",
    "pi.claim_verdict",
    "researcher.execute_task",
    "researcher.summarize_source",
    # The new loop's invocation types pick from real recorded inquiries /
    # evidence; benching is scoped by which task we replay.
    "researcher.plan_inquiry",
    "researcher.extract_evidence",
    "researcher.synthesize",
    "researcher.gap_check",
    "researcher.interpret_experiment",
    "researcher.parse_pricing",
}

_PROVIDER_BY_NAME = {p.value: p for p in Provider}


# -------------------------------------------------------------------------
# Memory wrappers — keep prompt-building alive even if Zep is down. The bench
# is not worth failing over a degraded nice-to-have; recall just returns empty.
# -------------------------------------------------------------------------
class _SafeMemory:
    def __init__(self, inner):
        self._inner = inner

    async def recent(self, session_id, k=5):
        try:
            return await self._inner.recent(session_id=session_id, k=k)
        except Exception as e:  # noqa: BLE001
            log.warning("bench: memory.recent failed (%s); returning []", e)
            return []

    async def recall_episodic(self, session_id, query, k=5):
        try:
            return await self._inner.recall_episodic(session_id=session_id, query=query, k=k)
        except Exception as e:  # noqa: BLE001
            log.warning("bench: memory.recall_episodic failed (%s); returning []", e)
            return []


class _NullMemory:
    async def recent(self, *a, **k):
        return []

    async def recall_episodic(self, *a, **k):
        return []


# -------------------------------------------------------------------------
# Engine — built once per process, cached on app.state.
# -------------------------------------------------------------------------
class BenchEngine:
    def __init__(self, pool, state, memory, lessons, curator, router_, schemas):
        self.pool = pool
        self.state = state
        self.memory = memory
        self.lessons = lessons
        self.curator = curator
        self.router = router_
        self.schemas = schemas  # {class_name: BaseModel subclass}

    @classmethod
    async def create(cls, pool) -> BenchEngine:
        state = PostgresClient(pool=pool)
        try:
            raw = ZepClient.from_env()
            await raw.ensure_user()
            for s in ZEP_SESSIONS:
                await raw.ensure_session(s)
            memory = _SafeMemory(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("bench: Zep unavailable (%s); recall disabled", e)
            memory = _NullMemory()
        lessons = LessonsClient(pool=pool)
        curator = Curator(state=state, memory=memory, lessons=lessons)
        router_ = Router(
            pool=pool,
            gpu_lock=GPULock(),
            ollama_url=OLLAMA_URL,
            call_timeout_s=300.0,
            cloud_chain=build_cloud_chain(os.environ),
            premium_chain=build_premium_chain(os.environ),
        )
        return cls(pool, state, memory, lessons, curator, router_, _build_schema_registry())


def _build_schema_registry() -> dict[str, type[BaseModel]]:
    """Collect every Pydantic output schema reachable from the handler modules,
    keyed by class name (which is what recipe.output_schema stores)."""
    import labfoundry.bootstrap as bootstrap
    from labfoundry.adversarial import schemas as adversarial_schemas_mod
    from labfoundry.audit import schemas as audit_schemas_mod
    from labfoundry.harness import curator as curator_mod
    from labfoundry.planner import schemas as planner_schemas_mod
    from labfoundry.research import schemas as research_schemas_mod
    from labfoundry.research.experiments import fetch_pricing as fp_mod

    mods = [
        bootstrap,
        curator_mod,
        researcher,
        critic,
        claim_invalidated,
        queue_empty,
        phase_adjudicator,
        phase_transition,
        reflection,
        task_completed,
        research_schemas_mod,
        fp_mod,
        audit_schemas_mod,
        adversarial_schemas_mod,
        planner_schemas_mod,
    ]
    reg: dict[str, type[BaseModel]] = {}
    for mod in mods:
        for name, obj in vars(mod).items():
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
                reg.setdefault(name, obj)
    return reg


async def get_engine(app) -> BenchEngine:
    eng = getattr(app.state, "bench_engine", None)
    if eng is None:
        eng = await BenchEngine.create(app.state.pool)
        app.state.bench_engine = eng
    return eng


# -------------------------------------------------------------------------
# Context factory — assemble the real `context` dict each task_data_builder
# expects, from live data where possible. Raises BenchContextError with a
# human message when the prerequisite data doesn't exist yet.
# -------------------------------------------------------------------------
class BenchContextError(Exception):
    pass


async def _any_active_claim_id(state) -> int | None:
    theses = await state.get_active_claims(limit=1)
    return theses[0].id if theses else None


async def build_context(engine: BenchEngine, invocation_type: str, claim_id: int | None):
    state, pool = engine.state, engine.pool

    if invocation_type in ("pi.exploration_kickoff", "planner.generate_tasks"):
        return {}, "Built from global company state — no extra context."

    if invocation_type == "critic.kill_verdict":
        tid = claim_id or await _any_active_claim_id(state)
        if tid is None:
            raise BenchContextError("No active thesis to evaluate.")
        ctx = {"claim_id": tid}
        fid = await pool.fetchval(
            "SELECT id FROM findings WHERE claim_id=$1 ORDER BY relevance_score DESC NULLS LAST LIMIT 1", tid
        )
        if fid:
            ctx["triggering_finding_id"] = fid
        return ctx, f"Thesis T{tid}" + (f", triggered by finding F{fid}" if fid else "")

    if invocation_type == "pi.claim_verdict":
        tid = claim_id or await _any_active_claim_id(state)
        if tid is None:
            raise BenchContextError("No active thesis.")
        vid = await pool.fetchval(
            "SELECT id FROM critic_verdicts WHERE claim_id=$1 ORDER BY created_at DESC LIMIT 1", tid
        )
        if vid is None:
            vid = await pool.fetchval("SELECT id FROM critic_verdicts ORDER BY created_at DESC LIMIT 1")
        if vid is None:
            raise BenchContextError("No critic verdict exists yet to act on.")
        return {"claim_id": tid, "critic_verdict_id": vid}, f"Thesis T{tid}, verdict #{vid}"

    if invocation_type in ("researcher.execute_task", "researcher.summarize_source"):
        task_id = await pool.fetchval(
            "SELECT id FROM tasks WHERE ($1::bigint IS NULL OR claim_id=$1) ORDER BY created_at DESC LIMIT 1", claim_id
        )
        if task_id is None:
            raise BenchContextError("No tasks exist to run.")
        ctx = {"task_id": task_id}
        rows = await pool.fetch("SELECT title, summary FROM findings WHERE task_id=$1 LIMIT 5", task_id)
        if not rows and claim_id:
            rows = await pool.fetch(
                "SELECT title, summary FROM findings WHERE claim_id=$1 ORDER BY created_at DESC LIMIT 5", claim_id
            )
        if rows:
            ctx["raw_material"] = "\n\n".join(f"{r['title']}\n{r['summary']}" for r in rows)
        return ctx, f"Task #{task_id}" + (f" + {len(rows)} findings as raw material" if rows else "")

    if invocation_type == "evaluation.slop_score":
        task_id = await pool.fetchval(
            "SELECT task_id FROM findings GROUP BY task_id ORDER BY MAX(created_at) DESC LIMIT 1"
        )
        if task_id is None:
            raise BenchContextError("No findings exist to audit.")
        ids = [
            r["id"]
            for r in await pool.fetch("SELECT id FROM findings WHERE task_id=$1 ORDER BY created_at LIMIT 8", task_id)
        ]
        task = await state.get_task(task_id)
        findings = await state.get_findings(ids)
        return {"task": task, "findings": findings}, f"Task #{task_id}, {len(findings)} findings"

    if invocation_type == "reflect.lesson_propose":
        row = await pool.fetchrow(
            "SELECT invocation_type, output_summary FROM agent_runs "
            "WHERE status='completed' AND output_summary IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        if row is None:
            raise BenchContextError("No completed agent run to reflect on.")
        return (
            {"invocation_type": row["invocation_type"], "run_summary": row["output_summary"]},
            f"Reflecting on a {row['invocation_type']} run",
        )

    if invocation_type == "pi.phase_transition_ratify":
        s = await state.get_company_state()
        nxt = {"exploration": "convergence", "convergence": "commitment", "commitment": "execution"}.get(
            s.current_phase, "convergence"
        )
        return (
            {
                "from_phase": s.current_phase,
                "target_phase": nxt,
                "adjudicator_reasoning": (
                    "(bench) The adjudicator flags that phase maturity "
                    "criteria appear met and the phase may be ready to advance."
                ),
                "forced": False,
            },
            f"{s.current_phase} → {nxt}",
        )

    if invocation_type == "phase_adjudicator.check":
        s = await state.get_company_state()
        theses = await state.get_active_claims(limit=10)
        days = (datetime.now(UTC) - s.phase_started_at).days
        summary = "\n".join(f"- T{t.id} (conf {t.confidence:.2f}): {t.claim}" for t in theses) or "(none)"
        return (
            {"current_phase": s.current_phase, "days_in_phase": days, "theses_summary": summary},
            f"Phase {s.current_phase}, day {days}, {len(theses)} theses",
        )

    if invocation_type == "pi.spawn_claim":
        kid = await pool.fetchval(
            "SELECT id FROM claims WHERE status IN ('invalidated','merged') "
            "ORDER BY invalidated_at DESC NULLS LAST LIMIT 1"
        )
        if kid is None:
            raise BenchContextError("No killed thesis to replace.")
        return {"invalidated_claim_id": kid}, f"Replacing killed thesis T{kid}"

    # -- New agentic researcher loop bench contexts -----------------------

    if invocation_type == "researcher.extract_evidence":
        # Replay a real (task, sub_question, page) triple so the bench measures
        # apples-to-apples extraction quality. Walks: pick a recent evidence
        # row, look up the sub_question that produced it from the inquiry, and
        # pull the page content from the fetch_cache. Falls back to a task
        # whose inquiry has at least one cached page if the latest doesn't fit.
        row = await pool.fetchrow(
            """
            SELECT e.task_id, e.inquiry_id, e.sub_question_idx, e.url, e.title,
                   ri.sub_questions, fc.content
            FROM evidence e
            JOIN research_inquiries ri ON ri.id = e.inquiry_id
            JOIN fetch_cache fc ON fc.url = e.url
            WHERE ($1::bigint IS NULL OR e.task_id IN
                   (SELECT id FROM tasks WHERE claim_id = $1))
            ORDER BY e.id DESC LIMIT 1
            """,
            claim_id,
        )
        if row is None:
            raise BenchContextError("No cached page + evidence pair to replay. Run the demo loop first.")
        sub_qs = row["sub_questions"]
        if isinstance(sub_qs, str):
            sub_qs = json.loads(sub_qs)
        sq = sub_qs[row["sub_question_idx"]]
        return (
            {
                "task_id": row["task_id"],
                "sub_question": sq["q"],
                "url": row["url"],
                "title": row["title"] or "",
                "content": row["content"],
            },
            f"Task #{row['task_id']} · SQ{row['sub_question_idx']} · {(row['title'] or row['url'])[:60]}",
        )

    if invocation_type == "researcher.plan_inquiry":
        task_id = await pool.fetchval(
            "SELECT id FROM tasks WHERE ($1::bigint IS NULL OR claim_id=$1) ORDER BY created_at DESC LIMIT 1", claim_id
        )
        if task_id is None:
            raise BenchContextError("No tasks to plan against.")
        desc = await pool.fetchval("SELECT description FROM tasks WHERE id=$1", task_id)
        return (
            {"task_id": task_id, "question": desc, "iteration": 1, "prior_evidence": []},
            f"Task #{task_id} · iteration 1 (no prior evidence)",
        )

    if invocation_type == "researcher.synthesize":
        # Replay a recent task that has both inquiries and evidence rows.
        row = await pool.fetchrow(
            """
            SELECT ri.task_id, ri.question, ri.sub_questions
            FROM research_inquiries ri
            JOIN evidence e ON e.task_id = ri.task_id
            WHERE ($1::bigint IS NULL OR ri.task_id IN
                   (SELECT id FROM tasks WHERE claim_id = $1))
            GROUP BY ri.id ORDER BY ri.id DESC LIMIT 1
            """,
            claim_id,
        )
        if row is None:
            raise BenchContextError("No inquiry+evidence pair to synthesize.")
        sub_qs = row["sub_questions"]
        if isinstance(sub_qs, str):
            sub_qs = json.loads(sub_qs)
        ev_rows = await pool.fetch(
            """
            SELECT id, sub_question_idx, url, title, quote, claim, stance, confidence
            FROM evidence WHERE task_id=$1 ORDER BY id LIMIT 30
            """,
            row["task_id"],
        )
        evidence = [
            {
                "id": r["id"],
                "sub_question_idx": r["sub_question_idx"],
                "url": r["url"],
                "title": r["title"] or "",
                "quote": r["quote"],
                "claim": r["claim"],
                "stance": r["stance"],
                "confidence": float(r["confidence"]),
            }
            for r in ev_rows
        ]
        return (
            {
                "task_id": row["task_id"],
                "question": row["question"],
                "sub_questions": [sq["q"] for sq in sub_qs],
                "evidence": evidence,
                "experiments": [],
            },
            f"Task #{row['task_id']} · {len(evidence)} evidence items",
        )

    # Recipe exists but we have no tailored context — try empty and let it ride.
    return {}, "No tailored context (empty)."


# -------------------------------------------------------------------------
# /bench/options
# -------------------------------------------------------------------------
@router.get("/options")
async def options(request: Request) -> dict:
    engine = await get_engine(request.app)

    tasks = []
    for itype, recipe in sorted(RECIPES.items()):
        schema_name = recipe.output_schema
        runnable = schema_name in engine.schemas
        tasks.append(
            {
                "invocation_type": itype,
                "tier": ROUTE.get(itype, Tier.WORKHORSE).value if itype in ROUTE else "unknown",
                "agent": itype.split(".")[0],
                "output_schema": schema_name,
                "accepts_thesis": itype in THESIS_SCOPED,
                "runnable": runnable,
            }
        )

    # Local Ollama models
    models = []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            for m in r.json().get("models", []):
                name = m.get("name")
                if name:
                    models.append({"id": f"ollama:{name}", "provider": "ollama", "model_name": name, "location": "local"})
    except Exception as e:  # noqa: BLE001
        log.warning("bench: could not list Ollama models (%s)", e)

    # Cloud models from the configured chains (premium first, then free)
    seen = set()
    for cp in engine.router.premium_chain + engine.router.cloud_chain:
        mid = f"{cp.provider.value}:{cp.model_name}"
        if mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "provider": cp.provider.value, "model_name": cp.model_name, "location": "cloud"})

    theses = [{"id": t.id, "claim": t.claim} for t in await engine.state.get_active_claims(limit=25)]

    return {"tasks": tasks, "models": models, "theses": theses}


# -------------------------------------------------------------------------
# /bench/run
# -------------------------------------------------------------------------
class BenchModelRef(BaseModel):
    provider: str
    model_name: str


class BenchRunRequest(BaseModel):
    invocation_type: str
    models: list[BenchModelRef]
    claim_id: int | None = None


def _jobs(app) -> dict:
    jobs = getattr(app.state, "bench_jobs", None)
    if jobs is None:
        jobs = {}
        app.state.bench_jobs = jobs
    return jobs


def _prune(jobs: dict, keep: int = 20) -> None:
    # Drop oldest finished jobs once we exceed `keep` (insertion-ordered dict).
    while len(jobs) > keep:
        jobs.pop(next(iter(jobs)))


@router.post("/run")
async def run(req: BenchRunRequest, request: Request) -> dict:
    """Start a comparison and return a job id immediately. Each model runs in
    the background; the client polls GET /bench/jobs/{id} for results as they
    land. This keeps every HTTP call short — a 70s synchronous response would
    otherwise be reset by the dev proxy / browser."""
    engine = await get_engine(request.app)

    recipe = RECIPES.get(req.invocation_type)
    if recipe is None:
        return {"error": f"Unknown task {req.invocation_type!r}."}
    schema_cls = engine.schemas.get(recipe.output_schema)
    if schema_cls is None:
        return {
            "error": f"Task {req.invocation_type} has no resolvable output schema ({recipe.output_schema}); not runnable."
        }

    try:
        ctx, ctx_note = await build_context(engine, req.invocation_type, req.claim_id)
        prompt = await engine.curator.build(req.invocation_type, ctx)
    except BenchContextError as e:
        return {"error": f"Can't ground this task yet: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("bench: prompt build failed")
        return {"error": f"Prompt build failed: {type(e).__name__}: {e}"}

    tier = ROUTE.get(req.invocation_type, Tier.WORKHORSE)
    temperature = MODELS[tier].temperature if tier in MODELS else 0.3

    job_id = uuid.uuid4().hex[:12]
    results: dict[str, dict] = {
        f"{m.provider}:{m.model_name}": {
            "provider": m.provider,
            "model_name": m.model_name,
            "status": "pending",
        }
        for m in req.models
    }
    job = {
        "status": "running",
        "invocation_type": req.invocation_type,
        "tier": tier.value,
        "output_schema": recipe.output_schema,
        "context_note": ctx_note,
        "prompt_tokens": prompt.total_tokens,
        "prompt_preview": prompt.as_system_message()[:12000],
        "results": results,
    }
    jobs = _jobs(request.app)
    jobs[job_id] = job
    _prune(jobs)

    async def run_one(m: BenchModelRef) -> None:
        key = f"{m.provider}:{m.model_name}"
        provider = _PROVIDER_BY_NAME.get(m.provider)
        if provider is None:
            results[key] = {
                **results[key],
                "status": "error",
                "latency_ms": 0,
                "error": f"Unknown provider {m.provider!r}",
            }
            return
        t0 = time.perf_counter()
        try:
            text, out_tokens = await engine.router.run_single(
                prompt,
                schema_cls,
                provider,
                m.model_name,
                temperature=temperature,
                timeout_s=240.0,
            )
            dt = int((time.perf_counter() - t0) * 1000)
            parsed = None
            with contextlib.suppress(Exception):
                parsed = json.loads(text)
            # Run it through the real Pydantic schema too: shows whether this
            # model's output is even usable live, and surfaces what the system
            # would actually consume after normalization (e.g. a weaken verdict
            # gets its missing delta filled in).
            valid, validated, verr = False, None, None
            try:
                validated = schema_cls.model_validate_json(text).model_dump(mode="json")
                valid = True
            except Exception as e:  # noqa: BLE001
                verr = str(e)[:200]
            results[key] = {
                **results[key],
                "status": "ok",
                "latency_ms": dt,
                "output_tokens": out_tokens,
                "parsed": parsed,
                "raw": None if parsed is not None else text[:4000],
                "valid": valid,
                "validated": validated,
                "validation_error": verr,
            }
        except Exception as e:  # noqa: BLE001
            dt = int((time.perf_counter() - t0) * 1000)
            results[key] = {
                **results[key],
                "status": "error",
                "latency_ms": dt,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            }

    async def worker() -> None:
        try:
            await asyncio.gather(*[run_one(m) for m in req.models])
        finally:
            job["status"] = "done"
            # Persist so the comparison can be retrieved later (the in-memory
            # job is pruned; this row is permanent).
            try:
                async with engine.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO bench_runs
                          (job_id, completed_at, invocation_type, tier, claim_id,
                           context_note, prompt_tokens, prompt_preview, status, results)
                        VALUES ($1, NOW(), $2, $3, $4, $5, $6, $7, 'done', $8)
                        """,
                        job_id,
                        req.invocation_type,
                        tier.value,
                        req.claim_id,
                        ctx_note,
                        prompt.total_tokens,
                        job["prompt_preview"],
                        list(results.values()),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("bench: failed to persist run %s (%s)", job_id, e)

    asyncio.create_task(worker())

    return {
        "job_id": job_id,
        "invocation_type": req.invocation_type,
        "tier": tier.value,
        "output_schema": recipe.output_schema,
        "context_note": ctx_note,
        "prompt_tokens": prompt.total_tokens,
        "prompt_preview": prompt.as_system_message()[:12000],
        "status": "running",
        "results": list(results.values()),
    }


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request) -> dict:
    job = _jobs(request.app).get(job_id)
    if job is None:
        return {"error": "Job expired or unknown.", "status": "gone"}
    return {"status": job["status"], "results": list(job["results"].values())}


@router.get("/runs")
async def list_runs(request: Request, limit: int = 30) -> dict:
    """Persisted comparison history — newest first, with a per-model summary."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, created_at, invocation_type, tier, context_note, status, results
            FROM bench_runs ORDER BY created_at DESC LIMIT $1
            """,
            min(limit, 200),
        )
    runs = []
    for r in rows:
        results = r["results"] or []
        runs.append(
            {
                "id": r["id"],
                "created_at": r["created_at"].isoformat(),
                "invocation_type": r["invocation_type"],
                "tier": r["tier"],
                "context_note": r["context_note"],
                "status": r["status"],
                "models": [
                    {
                        "provider": x.get("provider"),
                        "model_name": x.get("model_name"),
                        "status": x.get("status"),
                        "latency_ms": x.get("latency_ms"),
                        "valid": x.get("valid"),
                    }
                    for x in results
                ],
            }
        )
    return {"runs": runs}


class ReplayStepRequest(BaseModel):
    run_id: int
    model: BenchModelRef  # caller picks the target (UI defaults to original)
    prompt_override: str | None = None  # full text replacement; if absent, use input_summary verbatim


@router.post("/replay-step")
async def replay_step(req: ReplayStepRequest, request: Request) -> dict:
    """Re-run a single agent_run with a frozen prompt against a chosen model.

    Loads the original `input_summary` (the assembled system message exactly
    as the model saw it) and feeds it to one explicit provider+model — no
    curator rebuild (no live ctx re-fetch), no fallback chain, no caps
    charged, no persistence into the live loop. Caller controls the target
    model so 'what would gpt-4o do on this exact prompt' is a one-click test.

    Pass `prompt_override` to hand-edit the prompt text before sending.
    """
    engine = await get_engine(request.app)

    async with engine.pool.acquire() as conn:
        orig = await conn.fetchrow(
            """
            SELECT id, invocation_type, model_tier, model_name, step_name,
                   input_summary, output_summary, started_at, completed_at,
                   input_token_count, output_token_count, status, error
            FROM agent_runs WHERE id = $1
            """,
            req.run_id,
        )
    if orig is None:
        return {"error": f"agent_run #{req.run_id} not found"}
    if not orig["input_summary"]:
        return {"error": "agent_run has no input_summary (pre-trace row); can't replay frozen"}

    invocation_type = orig["invocation_type"]
    recipe = RECIPES.get(invocation_type)
    if recipe is None:
        return {"error": f"no recipe for invocation_type {invocation_type!r}"}
    schema_cls = engine.schemas.get(recipe.output_schema)
    if schema_cls is None:
        return {"error": f"schema {recipe.output_schema!r} not resolvable; can't validate replay"}

    provider = _PROVIDER_BY_NAME.get(req.model.provider)
    if provider is None:
        return {"error": f"unknown provider {req.model.provider!r}"}

    system_text = req.prompt_override if req.prompt_override is not None else orig["input_summary"]

    from labfoundry.harness.curator import BuiltPrompt, PromptLayer

    prompt = BuiltPrompt(
        layers=[PromptLayer(name="frozen", content=system_text, priority=1)],
        tool_names=[],
        output_schema=recipe.output_schema,
        lesson_ids=[],
        # Rough char-based estimate. Token count on a replay is informational
        # only — no caps charged, no routing decisions made from it.
        total_tokens=len(system_text) // 4,
        invocation_type=invocation_type,
    )

    tier = ROUTE.get(invocation_type, Tier.WORKHORSE)
    temperature = MODELS[tier].temperature if tier in MODELS else 0.3

    t0 = time.perf_counter()
    try:
        text, out_tokens = await engine.router.run_single(
            prompt,
            schema_cls,
            provider,
            req.model.model_name,
            temperature=temperature,
            timeout_s=240.0,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "provider": provider.value,
            "model_name": req.model.model_name,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "original": _replay_original(orig),
            "frozen": req.prompt_override is None,
        }

    parsed = None
    with contextlib.suppress(Exception):
        parsed = json.loads(text)
    valid, validated, verr = False, None, None
    try:
        validated = schema_cls.model_validate_json(text).model_dump(mode="json")
        valid = True
    except Exception as e:  # noqa: BLE001
        verr = str(e)[:200]

    return {
        "status": "ok",
        "latency_ms": latency_ms,
        "output_tokens": out_tokens,
        "provider": provider.value,
        "model_name": req.model.model_name,
        "raw": text if parsed is None else None,
        "parsed": parsed,
        "valid": valid,
        "validated": validated,
        "validation_error": verr,
        "original": _replay_original(orig),
        "frozen": req.prompt_override is None,
    }


def _replay_original(row) -> dict:
    """Side-by-side comparison shape: what the live run produced for this step."""
    started, completed = row["started_at"], row["completed_at"]
    latency_ms = int((completed - started).total_seconds() * 1000) if started and completed else None
    parsed_orig = None
    if row["output_summary"]:
        with contextlib.suppress(Exception):
            parsed_orig = json.loads(row["output_summary"])
    return {
        "run_id": row["id"],
        "step_name": row["step_name"],
        "invocation_type": row["invocation_type"],
        "model_name": row["model_name"],
        "tier": row["model_tier"],
        "status": row["status"],
        "latency_ms": latency_ms,
        "input_tokens": row["input_token_count"],
        "output_tokens": row["output_token_count"],
        "input_summary": row["input_summary"],
        "output_summary": row["output_summary"],
        "parsed": parsed_orig,
        "error": row["error"],
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: int, request: Request) -> dict:
    async with request.app.state.pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM bench_runs WHERE id = $1", run_id)
    if r is None:
        return {"error": "Run not found."}
    return {
        "id": r["id"],
        "created_at": r["created_at"].isoformat(),
        "invocation_type": r["invocation_type"],
        "tier": r["tier"],
        "context_note": r["context_note"],
        "prompt_tokens": r["prompt_tokens"],
        "prompt_preview": r["prompt_preview"],
        "status": r["status"],
        "results": r["results"] or [],
    }
