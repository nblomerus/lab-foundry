"""
Phase budget exceeded handler — triggered by the watchdog when a phase
overruns its budget by 1.5×.

Emits a *forced* phase.transition_proposed to the next sequential phase.
The PI still ratifies separately, but staying past budget is now the
non-default — the PI must justify staying.

This is the forcing function that prevents the company from exploring
forever. A real founder doesn't get infinite time either.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


NEXT_PHASE = {
    "exploration": "convergence",
    "convergence": "commitment",
    "commitment": "execution",
    "execution": None,  # nothing forces execution further
}


async def handle_phase_budget_exceeded(event: dict, dispatcher) -> dict | None:
    payload = event.get("payload") or {}
    current_phase = payload.get("phase")
    elapsed_days = payload.get("elapsed_days")

    if current_phase is None:
        return {"skipped": True, "reason": "no phase in payload"}

    next_phase = NEXT_PHASE.get(current_phase)
    if next_phase is None:
        return {"skipped": True, "reason": f"no transition out of {current_phase}"}

    async with dispatcher.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (
                event_type, target_type, target_id, payload, dedup_key
            )
            VALUES (
                'phase.transition_proposed', 'phase', 0, $1::jsonb,
                'forced-' || $2 || '-' || EXTRACT(EPOCH FROM NOW())::bigint::text
            )
            ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING
            """,
            json.dumps(
                {
                    "from_phase": current_phase,
                    "to_phase": next_phase,
                    "reasoning": (
                        f"Phase budget exceeded ({elapsed_days} days). Forcing transition proposal. PI may still reject."
                    ),
                    "cited_claim_ids": [],
                    "forced": True,
                }
            ),
            current_phase,
        )

    await dispatcher.memory.write_message(
        session_id="phase-transitions",
        content=(
            f"Watchdog forced transition proposal: {current_phase} → {next_phase} "
            f"after {elapsed_days} days. PI will decide whether to ratify."
        ),
        role_type="system",
    )

    return {
        "forced_transition_proposed": True,
        "from_phase": current_phase,
        "to_phase": next_phase,
    }
