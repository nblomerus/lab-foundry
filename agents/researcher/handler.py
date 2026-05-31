"""
Researcher handler — triggered by 'task.created' events.

Two implementations live here, selected by `RESEARCHER_LOOP`:
  - `v2` (default): the agentic loop in `agents.researcher.loop` —
    plan_inquiry → per-sub-question (search + fetch + extract_evidence)
    → experiments → synthesize → gap_check → iterate (up to 2).
  - `legacy`: the original single-shot snippet summarizer. Kept for one
    rollout cycle so we can flip back via env var if the new loop misbehaves
    on the running labfoundry.

Both share the claim/persist scaffolding: claim one pending task with
SKIP LOCKED, run the implementation, then complete (or fail) the task. The
downstream `task.completed` audit path is identical for both.

Concurrent task.created events still spawn concurrent handler invocations;
the GPU lock + cloud-chain parallelism in the router decide actual parallelism.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from agents.researcher.tools import (
    search_hacker_news,
    search_reddit,
    search_web,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Legacy schema — kept here so the legacy path still runs on rollback.
# The new loop's richer schemas live in labfoundry/research/schemas.py.
# -------------------------------------------------------------------------


class FindingOut(BaseModel):
    source: Literal["hacker_news", "arxiv", "reddit", "web", "other"]
    url: str | None = None
    title: str
    summary: str = Field(..., max_length=2000)
    relevance_score: float = Field(..., ge=1.0, le=10.0)
    why_it_matters: str = Field(..., max_length=500)
    supports_thesis: bool | None = None


class ResearcherFindings(BaseModel):
    findings: list[FindingOut] = Field(default_factory=list)


# -------------------------------------------------------------------------
# Top-level handler — selects v2 (default) or legacy by env var
# -------------------------------------------------------------------------


async def handle_task_created(event: dict, dispatcher) -> dict | None:
    """
    Try to claim one pending research task and execute it through whichever
    researcher implementation is selected. Idempotent: returns a skip result
    when no task is claimable.
    """
    worker_id = f"researcher-{uuid.uuid4().hex[:8]}"
    task = await dispatcher.state.claim_task(
        worker_id=worker_id,
        department="research",
    )
    if task is None:
        return {"skipped": True, "reason": "no claimable research task"}

    impl = os.environ.get("RESEARCHER_LOOP", "v2").lower()
    log.info("researcher %s claimed T%s (%s): %s", worker_id, task.id, impl, task.description[:80])

    if impl == "legacy":
        return await _legacy_handle_task_created(task, worker_id, event, dispatcher)
    return await _v2_handle_task_created(task, worker_id, event, dispatcher)


# -------------------------------------------------------------------------
# v2 — agentic loop (agents.researcher.loop.run_research_task)
# -------------------------------------------------------------------------


async def _v2_handle_task_created(task, worker_id: str, event: dict, dispatcher) -> dict:
    # Import is deferred so the legacy path still works if `research/loop.py`
    # has an import error in development.
    from agents.researcher.loop import run_research_task

    try:
        summary = await run_research_task(
            task=task,
            dispatcher=dispatcher,
            triggered_by_event_id=event["id"],
        )
    except Exception as e:  # noqa: BLE001 — task-level failure is non-fatal to harness
        log.exception("researcher v2 failed for T%s", task.id)
        await dispatcher.state.fail_task(task.id, error=f"researcher v2: {e}")
        return {"task_id": task.id, "failed": True, "reason": str(e)[:200]}

    if not summary["findings"]:
        # No findings is a legitimate result — the loop may have decided the
        # evidence didn't support any finding. We still complete the task so
        # the audit path runs (it short-circuits on zero findings).
        log.info("research T%s: zero findings (synthesis declined)", task.id)

    await dispatcher.state.complete_task(
        task_id=task.id,
        result={
            "worker": worker_id,
            "impl": "v2",
            "iterations": summary["iterations"],
            "inquiry_ids": summary["inquiry_ids"],
            "evidence_count": summary["evidence_count"],
            "experiments_run": summary["experiments_run"],
            "finding_ids": summary["findings"],
        },
    )

    return {
        "task_id": task.id,
        "worker": worker_id,
        "impl": "v2",
        "findings": len(summary["findings"]),
        "iterations": summary["iterations"],
        "evidence_count": summary["evidence_count"],
        "experiments_run": summary["experiments_run"],
    }


# -------------------------------------------------------------------------
# Legacy — preserved verbatim from the pre-loop implementation
# -------------------------------------------------------------------------

_LEGACY_SOURCE_TOOLS = {
    "hacker_news": search_hacker_news,
    "web": search_web,
    "reddit": search_reddit,
}


async def _legacy_gather_raw_material(
    query: str,
    sources: list[str],
    cap_chars: int = 15_000,
) -> str:
    """Original snippet-concat path. Kept verbatim for the legacy fallback."""
    chunks: list[str] = []
    per_source = max(2_000, cap_chars // max(1, len(sources)))
    for source in sources:
        tool = _LEGACY_SOURCE_TOOLS.get(source)
        if tool is None:
            continue
        try:
            results = await tool(query=query, limit=5)
        except Exception as e:  # noqa: BLE001
            log.warning("legacy research source %s failed for %r: %s", source, query, e)
            continue
        if not results:
            continue
        chunks.append(f"\n=== {source} — {len(results)} results ===\n")
        used = 0
        for r in results:
            block = f"- [{r.title}]({r.url})\n  {r.snippet}\n"
            if used + len(block) > per_source:
                break
            chunks.append(block)
            used += len(block)
    return "".join(chunks)


async def _legacy_handle_task_created(task, worker_id: str, event: dict, dispatcher) -> dict:
    payload = task.payload or {}
    query = payload.get("query") or task.description
    requested = payload.get("sources") or ["web"]
    sources = list(dict.fromkeys(["reddit", "hacker_news", *requested]))

    raw_material = await _legacy_gather_raw_material(query, sources)
    if not raw_material.strip():
        await dispatcher.state.fail_task(
            task.id,
            error="no raw material gathered from any source",
        )
        return {"task_id": task.id, "failed": True, "reason": "no raw material"}

    prompt = await dispatcher.curator.build(
        invocation_type="researcher.execute_task",
        context={"task_id": task.id, "raw_material": raw_material},
    )

    findings_out, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ResearcherFindings,
        triggered_by_event_id=event["id"],
    )

    finding_ids: list[int] = []
    for f in findings_out.findings:
        fid = await dispatcher.state.record_finding(
            task_id=task.id,
            claim_id=task.claim_id,
            source=f.source,
            url=f.url,
            title=f.title,
            summary=f.summary,
            relevance_score=f.relevance_score,
            why_it_matters=f.why_it_matters,
            supports_thesis=f.supports_thesis,
        )
        finding_ids.append(fid)

    await dispatcher.state.complete_task(
        task_id=task.id,
        result={
            "finding_count": len(finding_ids),
            "finding_ids": finding_ids,
            "run_id": run_id,
            "worker": worker_id,
            "impl": "legacy",
        },
    )

    return {
        "task_id": task.id,
        "worker": worker_id,
        "impl": "legacy",
        "findings": len(finding_ids),
        "run_id": run_id,
    }
