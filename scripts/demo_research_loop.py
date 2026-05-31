"""
End-to-end demo of the agentic researcher loop on the side instance.

Usage:

    # Bring up the side DB + searxng (one time)
    docker compose -f docker-compose.demo.yml up -d

    # Run the demo against a research question
    DATABASE_URL_DEMO=postgresql://boardroom:boardroom@localhost:5433/labfoundry \\
    SEARXNG_URL=http://localhost:8081 \\
    DEEPSEEK_API_KEY=... \\
    python scripts/demo_research_loop.py "Is there demand for self-hosted AI ops tooling?"

The script:
  1. Connects to the demo DB. Migrations auto-applied on container startup
     (mounted /docker-entrypoint-initdb.d) so tables exist.
  2. Seeds `company_state` + a thesis + one research task carrying the
     framing question (idempotent — re-runs reuse the existing seed unless
     `--fresh` is passed).
  3. Builds a minimal dispatcher (state + router + curator with stub
     memory/lessons) and calls `run_research_task` directly. No event bus,
     no full harness — keeps the demo deterministic.
  4. Prints structured output for each step (plan, per-page evidence,
     experiments, synthesis, gap_check) and the agent_run id for each.

After it finishes, point the web app at the demo DB
(`DATABASE_URL=postgresql://...:5433/labfoundry`) and open
`http://localhost:8088/debug/task/<task_id>` to walk the same tree in the UI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

# Force experiment kinds + curator recipes to register at import time.
import agents.researcher.experiments  # noqa: F401
import agents.researcher.loop  # noqa: F401
from agents.evaluation.handler import handle_task_completed
from agents.researcher.loop import run_research_task
from harness.curator import Curator
from harness.router import (
    GPULock,
    Router,
    build_cloud_chain,
    build_premium_chain,
)
from state.client import PostgresClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("demo")

DEMO_PROBLEM = (
    "Find a defensible niche for an autonomous AI-native research company "
    "whose product is high-quality decision-support intelligence for "
    "operators in adjacent technical markets."
)
DEMO_STANCE = "No slop. Concrete evidence only. Compete on depth, not breadth."
DEMO_SUCCESS = "One paying customer within 30 days of a committed charter."


# -------------------------------------------------------------------------
# Stubs for memory and lessons — the researcher recipes don't use either,
# but Curator wires them in unconditionally.
# -------------------------------------------------------------------------


class _NoopMemory:
    """Memory stub: every method is a no-op. The researcher loop's recipes
    have `recall_sessions=[]` so `_recall_layer` is never invoked."""

    async def recent(self, *_, **__):
        return []

    async def recall_episodic(self, *_, **__):
        return []

    async def write_message(self, *_, **__):
        return None

    async def ensure_user(self):
        return None

    async def ensure_session(self, *_, **__):
        return None


class _NoopLessons:
    """Lessons stub returning no applicable lessons."""

    async def fetch_applicable(self, **_):
        return []


# -------------------------------------------------------------------------
# Minimal dispatcher
# -------------------------------------------------------------------------


class _Dispatcher:
    def __init__(self, state, router, curator):
        self.state = state
        self.router = router
        self.curator = curator
        self.memory = _NoopMemory()


# -------------------------------------------------------------------------
# Seed bootstrap
# -------------------------------------------------------------------------


async def _ensure_seed(pool: asyncpg.Pool, question: str, fresh: bool) -> tuple[int, int]:
    """Seed company_state, an exploratory thesis, and one research task.

    Returns (thesis_id, task_id). Idempotent: with `fresh=False`, reuses an
    existing task carrying the same description if present.
    """
    async with pool.acquire() as conn:
        if fresh:
            await conn.execute(
                "TRUNCATE evidence, experiment_runs, research_inquiries, "
                "findings, tasks, theses, fetch_cache, events, agent_runs, "
                "cost_tracking, deepseek_balance_log RESTART IDENTITY CASCADE"
            )

        present = await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1")
        if not present:
            deadline = datetime.now(UTC) + timedelta(days=30)
            await conn.execute(
                """
                INSERT INTO company_state (
                    id, problem_statement, stance, success_criterion,
                    current_phase, deadline
                )
                VALUES (1, $1, $2, $3, 'exploration', $4)
                """,
                DEMO_PROBLEM,
                DEMO_STANCE,
                DEMO_SUCCESS,
                deadline,
            )
            log.info("seeded company_state")

        thesis_id = await conn.fetchval(
            "SELECT id FROM theses WHERE claim = $1",
            question,
        )
        if thesis_id is None:
            thesis_id = await conn.fetchval(
                """
                INSERT INTO theses (claim, confidence)
                VALUES ($1, 0.50)
                RETURNING id
                """,
                question,
            )
            log.info("created thesis T%s", thesis_id)

        task_id = await conn.fetchval(
            "SELECT id FROM tasks WHERE description = $1 AND status = 'pending'",
            question,
        )
        if task_id is None:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    thesis_id, department, task_type, description,
                    payload, priority, status
                )
                VALUES ($1, 'research', 'investigate', $2, '{}'::jsonb, 5, 'pending')
                RETURNING id
                """,
                thesis_id,
                question,
            )
            log.info("created task T%s", task_id)
        else:
            log.info("reusing pending task T%s", task_id)

    return thesis_id, task_id


# -------------------------------------------------------------------------
# Pretty output
# -------------------------------------------------------------------------


def _hr(title: str = ""):
    bar = "=" * 78
    if title:
        print(f"\n{bar}\n  {title}\n{bar}")
    else:
        print(bar)


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


async def _print_tree(state: PostgresClient, task_id: int):
    tree = await state.get_research_tree(task_id)
    _hr(f"FINAL TREE — task T{task_id}")
    print(f"task: {tree['task']['description']}")
    print(f"status: {tree['task']['status']}")
    print(
        f"inquiries: {len(tree['inquiries'])}  evidence: {len(tree['evidence'])}  "
        f"experiments: {len(tree['experiments'])}  findings: {len(tree['findings'])}"
    )

    for inq in tree["inquiries"]:
        _hr(f"  iteration {inq['iteration']}: {inq['question'][:120]}")
        print("  sub-questions:")
        for i, sq in enumerate(inq["sub_questions"]):
            print(f"    [{i}] ({','.join(sq.get('sources') or [])}) {sq['q']}")
            print(f"        why: {sq.get('why', '')}")
        if inq["proposed_experiments"]:
            print("  proposed experiments:")
            for pe in inq["proposed_experiments"]:
                print(f"    - {pe['kind']}: {pe.get('why', '')[:100]}")

        # Evidence for this inquiry
        ev_here = [e for e in tree["evidence"] if e["inquiry_id"] == inq["id"]]
        if ev_here:
            print(f"  evidence ({len(ev_here)}):")
            for e in ev_here:
                print(
                    f"    [SQ{e['sub_question_idx']}, {e['stance']}, conf {float(e['confidence']):.2f}] {e['url'][:80]}"
                )
                print(f"        claim: {e['claim']}")
                print(f'        quote: "{e["quote"][:200]}"')
        exp_here = [x for x in tree["experiments"] if x["inquiry_id"] == inq["id"]]
        if exp_here:
            print(f"  experiments ({len(exp_here)}):")
            for x in exp_here:
                print(f"    - {x['kind']} [{x['status']}]")
                if x.get("interpretation"):
                    print(f"        {x['interpretation'][:240]}")
                if x.get("error"):
                    print(f"        error: {x['error'][:200]}")

    if tree["findings"]:
        _hr("  FINDINGS (written to findings table)")
        for f in tree["findings"]:
            print(f"  F{f['id']} [{f['source']}, rel {float(f['relevance_score']):.1f}, supports={f['supports_thesis']}]")
            print(f"    {f['title']}")
            print(f"    {f['summary'][:300]}")


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------


async def main(question: str, fresh: bool) -> int:
    db_url = os.environ.get("DATABASE_URL_DEMO") or os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL_DEMO (or DATABASE_URL) must be set")
        return 1
    if "localhost:5432" in db_url:
        log.warning("DATABASE_URL points at the LIVE labfoundry DB (5432); refusing")
        log.warning("Set DATABASE_URL_DEMO to the side instance (default :5433)")
        return 1

    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    log.info("demo DB: %s", db_url)
    log.info("ollama:  %s", ollama_url)
    log.info("searxng: %s", os.environ.get("SEARXNG_URL", "http://localhost:8080"))

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=8)
    try:
        # Reap any agent_runs left in `running` state from a prior demo that
        # was killed mid-call (PID killed externally → no completed_at set).
        # 10 min is far longer than any legitimate single call.
        async with pool.acquire() as conn:
            reaped = await conn.fetchval(
                """
                WITH r AS (
                    UPDATE agent_runs
                    SET status = 'failed', completed_at = NOW(),
                        error = 'orphaned: prior process killed before completion (auto-reaped)'
                    WHERE status = 'running'
                      AND started_at < NOW() - INTERVAL '10 minutes'
                    RETURNING 1
                ) SELECT count(*) FROM r
                """,
            )
        if reaped:
            log.info("reaped %d orphaned running agent_runs from a prior kill", reaped)

        thesis_id, task_id = await _ensure_seed(pool, question, fresh=fresh)

        state = PostgresClient(pool=pool)
        curator = Curator(state=state, memory=_NoopMemory(), lessons=_NoopLessons())
        router = Router(
            pool=pool,
            gpu_lock=GPULock(),
            ollama_url=ollama_url,
            cloud_chain=build_cloud_chain(os.environ),
            premium_chain=build_premium_chain(os.environ),
        )
        dispatcher = _Dispatcher(state, router, curator)

        # Claim the task (sets status=running). The loop expects this.
        task = await state.claim_task(worker_id="demo-cli", department="research")
        if task is None or task.id != task_id:
            log.error("failed to claim task T%s (claimed=%s); aborting", task_id, task)
            return 2

        # Persist a task.created event so the research-tree endpoint can
        # round-trip-link every loop run back to this task via
        # `triggered_by_event_id`. Without this, synthesize and gap_check
        # don't appear in the tree's agent_runs list.
        async with pool.acquire() as conn:
            event_id = await conn.fetchval(
                """
                INSERT INTO events (event_type, target_type, target_id, payload, dedup_key)
                VALUES ('task.created', 'task', $1, '{}'::jsonb, $2)
                ON CONFLICT (event_type, target_type, target_id, dedup_key) DO UPDATE
                SET payload = events.payload
                RETURNING id
                """,
                task.id,
                f"demo-{task.id}",
            )

        _hr(f"running researcher loop on T{task.id}")
        print(f"question: {task.description}\n")

        summary = await run_research_task(
            task=task,
            dispatcher=dispatcher,
            triggered_by_event_id=event_id,
        )

        await state.complete_task(
            task_id=task.id,
            result={
                "impl": "v2-demo",
                **summary,
            },
        )

        # Manually fire the auditor (the demo script bypasses the event bus,
        # so task.completed has no consumer). With the new groundedness rubric,
        # the auditor reads both findings AND evidence.
        _hr("running auditor (slop + groundedness)")
        try:
            audit_summary = await handle_task_completed(
                {"id": event_id, "target_id": task.id},
                dispatcher,
            )
            print(_dump(audit_summary))
        except Exception as e:  # noqa: BLE001
            log.warning("auditor failed: %s", e)

        _hr("RUN SUMMARY")
        print(_dump({k: v for k, v in summary.items() if k != "trace"}))
        print("\nstep trace:")
        for step in summary["trace"]:
            print(f"  - {_dump(step)}")

        await _print_tree(state, task.id)

        web_base = os.environ.get("DEMO_WEB_BASE", "http://localhost:8088")
        _hr()
        print(f"View this tree in the UI at: {web_base}/debug/task/{task.id}")
        print(f"(make sure the web app's API points at {db_url})")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question", help="The research question to investigate.")
    ap.add_argument("--fresh", action="store_true", help="Wipe demo tables before running (destroys prior runs).")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.question, fresh=args.fresh)))
