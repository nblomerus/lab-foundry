"""
Researcher handler — triggered by 'task.created' events.

Flow:
  1. Claim one pending research task (SKIP LOCKED — handler may get nothing
     if another concurrent handler beat it).
  2. Gather raw material from the task's source list via the research tools.
  3. Build and invoke the researcher.execute_task prompt.
  4. Persist each finding via state.record_finding.
  5. Complete the task; the trigger emits task.completed downstream.

Concurrent task.created events spawn concurrent handler invocations. With
one GPU + one Ollama, effective parallelism is 1 due to the model lock,
but the swarm pattern is preserved for future cloud routing.
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field

from boardroom.mcp_servers.boardroom_research.tools import (
    fetch_url,
    search_hacker_news,
    search_reddit,
    search_web,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Researcher output schema
# -------------------------------------------------------------------------

class FindingOut(BaseModel):
    source: Literal["hacker_news", "arxiv", "reddit", "web", "other"]
    url: Optional[str] = None
    title: str
    summary: str = Field(..., max_length=2000)
    relevance_score: float = Field(..., ge=1.0, le=10.0)
    why_it_matters: str = Field(..., max_length=500)
    supports_thesis: Optional[bool] = None


class ResearcherFindings(BaseModel):
    findings: list[FindingOut] = Field(default_factory=list)


# -------------------------------------------------------------------------
# Source dispatch
# -------------------------------------------------------------------------

SOURCE_TOOLS = {
    "hacker_news": search_hacker_news,
    "web":         search_web,
    "reddit":      search_reddit,
}


async def _gather_raw_material(
    query: str,
    sources: list[str],
    cap_chars: int = 15_000,
) -> str:
    """Call each requested source's search; concatenate up to cap_chars."""
    chunks: list[str] = []
    total = 0
    for source in sources:
        tool = SOURCE_TOOLS.get(source)
        if tool is None:
            continue
        try:
            results = await tool(query=query, limit=5)
        except Exception as e:
            log.warning("research source %s failed for %r: %s", source, query, e)
            continue
        if not results:
            continue
        chunks.append(f"\n=== {source} — {len(results)} results ===\n")
        for r in results:
            block = f"- [{r.title}]({r.url})\n  {r.snippet}\n"
            if total + len(block) > cap_chars:
                break
            chunks.append(block)
            total += len(block)
        if total >= cap_chars:
            break
    return "".join(chunks)


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------

async def handle_task_created(event: dict, dispatcher) -> Optional[dict]:
    """
    Try to claim one pending research task and execute it.

    Returns dict describing what happened. Idempotent: if no task is
    claimable, returns a skip result rather than erroring.
    """
    worker_id = f"researcher-{uuid.uuid4().hex[:8]}"
    task = await dispatcher.state.claim_task(
        worker_id=worker_id, department="research",
    )
    if task is None:
        return {"skipped": True, "reason": "no claimable research task"}

    payload = task.payload or {}
    query = payload.get("query") or task.description
    sources = payload.get("sources", ["web", "hacker_news"])

    log.info("researcher %s claimed T%s: %s", worker_id, task.id, task.description[:80])

    raw_material = await _gather_raw_material(query, sources)
    if not raw_material.strip():
        await dispatcher.state.fail_task(
            task.id, error="no raw material gathered from any source",
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
            thesis_id=task.thesis_id,
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
            "finding_ids":   finding_ids,
            "run_id":        run_id,
            "worker":        worker_id,
        },
    )

    return {
        "task_id": task.id,
        "worker":  worker_id,
        "findings": len(finding_ids),
        "run_id":  run_id,
    }
