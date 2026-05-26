"""
Audit slop circuit-breaker handler — triggered by 'audit.slop_detected'.

When the Auditor's slop rate on a thesis exceeds 40% over 5+ findings, this
handler:

  1. Halts all pending research tasks for that thesis.
  2. Lowers thesis confidence by -0.20 (significant but recoverable).
  3. Narrates to the dissent session.

This is the "stop generating into broken state" lever from Zechner's
compound-error warning. Doesn't kill the thesis — that's the Adversary's
job. Slop indicates the research *approach* is broken, not necessarily
the thesis itself.

If the thesis recovers (better findings come in, confidence climbs back),
research resumes naturally when the Planner refills the queue.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


async def handle_audit_slop_detected(event: dict, dispatcher) -> Optional[dict]:
    thesis_id = event["target_id"]
    payload = event.get("payload") or {}
    slop_rate = float(payload.get("slop_rate", 0.0))

    # Halt pending research tasks for this thesis
    async with dispatcher.pool.acquire() as conn:
        halted = await conn.fetchval(
            """
            WITH halted AS (
                UPDATE tasks
                SET status = 'halted',
                    halt_reason = $1
                WHERE thesis_id = $2
                  AND status = 'pending'
                  AND department = 'research'
                RETURNING id
            )
            SELECT COUNT(*) FROM halted
            """,
            f"audit slop rate {slop_rate:.2f}",
            thesis_id,
        )

    # Lower thesis confidence (only if still active)
    new_conf_str: str
    try:
        thesis = await dispatcher.state.get_thesis(thesis_id)
        if thesis.status == "active":
            new_conf = max(0.0, thesis.confidence - 0.20)
            await dispatcher.state.update_thesis_confidence(
                thesis_id=thesis_id,
                new_confidence=new_conf,
                reason=f"slop circuit-breaker: rate={slop_rate:.2f}",
                run_id=None,
            )
            new_conf_str = f"{new_conf:.2f}"
        else:
            new_conf_str = "n/a (thesis not active)"
    except ValueError:
        new_conf_str = "n/a (thesis missing)"

    await dispatcher.memory.write_message(
        session_id="dissent",
        content=(
            f"Slop circuit-breaker tripped on T{thesis_id}. "
            f"Slop rate {slop_rate:.2f}. "
            f"Halted {halted or 0} pending tasks. "
            f"Confidence lowered to {new_conf_str}."
        ),
        role_type="system",
        metadata={"thesis_id": thesis_id, "slop_rate": slop_rate},
    )

    return {
        "thesis_id":      thesis_id,
        "tasks_halted":   halted or 0,
        "new_confidence": new_conf_str,
        "slop_rate":      slop_rate,
    }
