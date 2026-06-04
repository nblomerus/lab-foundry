"""
Mimir's DIRECTION channel — Mimir → scouts (MIMIR_WARDEN_SCOPE §5, demand side).

Discovery normally flows scouts → Mimir (sources bubble up). This is the reverse:
Mimir telling the scouts WHAT TO FOCUS ON. Two modes:

  STANDING focus (pull). The discovery sweep already steers itself by the agenda:
  when Ariadne (the PI) frames claims, `agents.mimir.collectors.plan_sweep` makes
  those claim statements the scout topics. So an active agenda *implicitly*
  directs the scouts; when Ariadne is dark the focus is the broad AI/ML frontier.

  DIRECTED focus (push). `request_focus()` lets the PI push specific topics ON
  DEMAND — it emits a directed `library.sweep_requested` so the scouts' next
  sweep targets exactly those topics. This is the explicit "Mimir, tell the
  scouts to go find X" lever, distinct from `request_acquire()` (which pulls ONE
  precise source). When Ariadne comes online and needs a specific area covered,
  she calls this and the scouts pivot.
"""

from __future__ import annotations

import hashlib
import logging

from agents.mimir.acquire import ALLOWED_REQUESTERS
from agents.mimir.collectors import ariadne_active, plan_sweep

log = logging.getLogger(__name__)

_MAX_FOCUS_TOPICS = 12


async def request_focus(state, *, topics: list[str], requester: str, why: str = "") -> dict:
    """Direct the scouts to focus their NEXT sweep on `topics` (Mimir → scouts).

    Emits a directed `library.sweep_requested` carrying explicit topics, which the
    Mimir sweep handler runs as-is. Allowed requesters mirror the acquire
    allow-list (the PI especially). Returns the topics actually dispatched."""
    if requester not in ALLOWED_REQUESTERS:
        raise ValueError(
            f"requester {requester!r} not allowed to direct scouts (allow-list: {sorted(ALLOWED_REQUESTERS)})"
        )
    topics = [t.strip() for t in (topics or []) if t and t.strip()][:_MAX_FOCUS_TOPICS]
    if not topics:
        return {"directed": [], "reason": "no topics"}
    key = hashlib.blake2b(("|".join([requester, *topics])).encode(), digest_size=7).hexdigest()
    await state.emit_corpus_event(
        "library.sweep_requested",
        target_type="ingest_source",
        payload={"topics": topics, "focus": True, "requested_by": requester, "why": why},
        dedup_key=f"focus-{key}",
    )
    log.info("mimir focus: %s directed scouts to %s", requester, topics)
    return {"directed": topics}


async def current_focus(state) -> dict:
    """What the scouts are currently focused on, and why: Ariadne's agenda (her
    active claims) when she's live, else the broad AI/ML frontier. This is the
    standing direction Mimir hands the scouts every sweep."""
    topics, _ = await plan_sweep(state)
    active = ariadne_active()
    return {
        "ariadne_active": active,
        "source": "agenda" if active else "frontier",
        "topics": topics,
    }
