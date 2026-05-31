"""
Graph sink handler — mirrors research lifecycle events into Neo4j.

Registered for:
  - claim.created → MERGE Claim node

Other graph writes are inlined into their respective handlers:
  - task_completed.py → merge_finding_grounds_claim (after audit pass)
  - critic.py → merge_critic_verdict_challenged_claim (after verdict creation)
  - phase_adjudicator.py → merge_claim (after confidence update)
  - claim_invalidated.py → merge_claim (after invalidation)

Failures are logged and swallowed: the graph is a read-optimized projection,
not the source of truth. A missed write degrades query quality but never
blocks the research loop.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle_graph_sink_claim_created(event: dict, dispatcher) -> dict | None:
    """Triggered by claim.created. Write the Claim node to the graph."""
    try:
        claim_id = event["target_id"]
        claim = await dispatcher.state.get_claim(claim_id)

        from labfoundry.mcp_servers.labfoundry_knowledge.tools import merge_claim

        await merge_claim(claim.id, claim.statement, claim.status, claim.confidence)
        return {"graph_written": True, "claim_id": claim_id}
    except Exception:
        log.exception("graph_sink: claim.created write failed — continuing")
        return {"graph_written": False}
