"""
Audit slop circuit-breaker handler — triggered by 'audit.slop_detected'.

When the Evaluation's slop rate on a claim exceeds 40% over 5+ findings, this
handler:

  1. Halts all pending research tasks for that claim.
  2. Lowers claim confidence by -0.20 (significant but recoverable).
  3. Narrates to the dissent session.

This is the "stop generating into broken state" lever from Zechner's
compound-error warning. Doesn't kill the claim — that's the Adversary's
job. Slop indicates the research *approach* is broken, not necessarily
the claim itself.

If the claim recovers (better findings come in, confidence climbs back),
research resumes naturally when the Planner refills the queue.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def handle_audit_slop_detected(event: dict, dispatcher) -> dict | None:
    claim_id = event["target_id"]
    payload = event.get("payload") or {}
    slop_rate = float(payload.get("slop_rate", 0.0))

    # Halt pending research tasks for this claim
    async with dispatcher.pool.acquire() as conn:
        halted = await conn.fetchval(
            """
            WITH halted AS (
                UPDATE tasks
                SET status = 'halted',
                    halt_reason = $1
                WHERE claim_id = $2
                  AND status = 'pending'
                  AND department = 'research'
                RETURNING id
            )
            SELECT COUNT(*) FROM halted
            """,
            f"audit slop rate {slop_rate:.2f}",
            claim_id,
        )

    # Lower claim confidence (only if still active)
    new_conf_str: str
    try:
        claim = await dispatcher.state.get_claim(claim_id)
        if claim.status in ("proposed", "tested", "weakly_supported", "replicated"):
            new_conf = max(0.0, claim.confidence - 0.20)
            await dispatcher.state.update_claim_confidence(
                claim_id=claim_id,
                new_confidence=new_conf,
                reason=f"slop circuit-breaker: rate={slop_rate:.2f}",
                run_id=None,
            )
            new_conf_str = f"{new_conf:.2f}"
        else:
            new_conf_str = "n/a (claim not active)"
    except ValueError:
        new_conf_str = "n/a (claim missing)"

    await dispatcher.memory.write_message(
        session_id="dissent",
        content=(
            f"Slop circuit-breaker tripped on T{claim_id}. "
            f"Slop rate {slop_rate:.2f}. "
            f"Halted {halted or 0} pending tasks. "
            f"Confidence lowered to {new_conf_str}."
        ),
        role_type="system",
        metadata={"claim_id": claim_id, "slop_rate": slop_rate},
    )

    return {
        "claim_id": claim_id,
        "tasks_halted": halted or 0,
        "new_confidence": new_conf_str,
        "slop_rate": slop_rate,
    }
