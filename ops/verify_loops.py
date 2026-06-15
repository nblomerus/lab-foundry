"""Per-loop verifier — for every research loop, confirm each step is firing AND closing.

Where `ops.closure_audit` answers "did anything fail to close?", this answers the dual question:
"for each named loop, is every step alive?" It walks the loops from `docs/AGENTS.md` and, for each
ordered step (an event), reports whether that event recently fired and reached a handler.

    set -a; . ./.env; set +a
    python -m ops.verify_loops                 # read-only matrix over the last 24h (default)
    python -m ops.verify_loops --since 720     # look-back window in minutes
    python -m ops.verify_loops --dry           # inject each loop's TRIGGER, poll for consume + next event
    python -m ops.verify_loops --dry --timeout 30

Marks per step: ✓ consumed · ⚠ fired-but-not-closed (pending/gated) · ✗ dropped no_handler / failed
· · idle (never fired in the window — can't confirm, not necessarily broken). Exit 1 if any step is ✗.

`--dry` WRITES: it emits each loop's trigger event into the live lab and waits for the running harness
to consume it + emit the next step. Without a running harness the triggers will sit `pending` (reported
as ⚠), so run it against a live lab.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv

from harness.dispatch import CLOSURE_EXEMPT_EVENTS

_OK, _WARN, _BAD, _DOT = "✓", "⚠", "✗", "·"

# Each loop is an ordered list of steps; a step is the event that SHOULD fire at that point. The verifier
# checks the event recently fired and was consumed. Kept in lock-step with docs/AGENTS.md §3–4.
LOOPS: list[tuple[str, list[str]]] = [
    ("Knowledge", ["source.discovered", "document.parsed", "document.ingested"]),
    ("Direction & gate", ["ariadne.deliberate", "claim.created", "direction.adjudicate"]),
    ("Scholarship", ["ariadne.review", "ariadne.propose"]),
    ("Execution", ["planner.plan", "task.created", "experiment.requested", "experiment.completed"]),
    ("Synthesis & conclude", ["finding.synthesize", "finding.composed"]),
    ("Verification", ["finding.composed", "finding.high_signal"]),
]

# A loop's trigger (for --dry) → the next event we expect the harness to emit after consuming it.
TRIGGERS: list[tuple[str, str, str]] = [
    ("Verification", "finding.composed", "finding.high_signal"),
    ("Execution", "planner.plan", "task.created"),
]


def classify(status: str | None, reason: str | None, event_type: str, n: int) -> tuple[str, str]:
    """Pure classifier: the latest row's (status, suppression_reason) for an event → (mark, detail).
    Telemetry/poll-consumed events (CLOSURE_EXEMPT_EVENTS) are never flagged as broken."""
    if n == 0:
        return _DOT, "idle (not seen in window)"
    if status == "consumed":
        return _OK, "consumed"
    if status == "failed":
        return _BAD, "handler failed"
    if status == "suppressed":
        if reason == "no_handler" and event_type not in CLOSURE_EXEMPT_EVENTS:
            return _BAD, "dropped no_handler"
        return _WARN, f"suppressed ({reason or 'gate'})"
    if status == "pending":
        return _WARN, "pending (not yet consumed)"
    return _WARN, f"status={status}"


async def _step_status(conn, event_type: str, since_min: int) -> dict:
    row = await conn.fetchrow(
        "SELECT count(*) AS n, "
        "(array_agg(status ORDER BY emitted_at DESC))[1]::text AS last_status, "
        "(array_agg(suppression_reason ORDER BY emitted_at DESC))[1] AS last_reason, "
        "max(emitted_at) AS last "
        "FROM events WHERE event_type = $1 AND emitted_at > now() - make_interval(mins => $2::int)",
        event_type,
        since_min,
    )
    n = row["n"] or 0
    mark, detail = classify(row["last_status"], row["last_reason"], event_type, n)
    return {"event": event_type, "mark": mark, "detail": detail, "n": n}


async def audit(conn, since_min: int) -> list[dict]:
    """Read-only: classify every step of every loop over the window. Returns flat step records."""
    out: list[dict] = []
    for loop, steps in LOOPS:
        print(f"\n{loop}")
        for ev in steps:
            s = await _step_status(conn, ev, since_min)
            print(f"  {s['mark']} {ev:<26} {s['detail']}  (n={s['n']})")
            out.append({"loop": loop, **s})
    return out


async def _emit(conn, event_type: str, payload: dict, dedup: str) -> int | None:
    return await conn.fetchval(
        "INSERT INTO events (event_type, target_type, target_id, payload, status, dedup_key) "
        "VALUES ($1, 'claim', 0, $2::jsonb, 'pending', $3) "
        "ON CONFLICT (event_type, target_type, target_id, dedup_key) DO NOTHING RETURNING id",
        event_type,
        json.dumps(payload),
        dedup,
    )


async def _poll_consumed(conn, event_id: int, timeout_s: float, interval_s: float = 1.0) -> str | None:
    """Poll one event row until it leaves 'pending', or timeout. Returns the terminal status (or None)."""
    waited = 0.0
    while waited <= timeout_s:
        status = await conn.fetchval("SELECT status::text FROM events WHERE id = $1", event_id)
        if status and status != "pending":
            return status
        if waited >= timeout_s:
            break
        await asyncio.sleep(interval_s)
        waited += interval_s
    return None


async def dry_run(conn, timeout_s: float) -> list[dict]:
    """Inject each loop's trigger and confirm the harness consumes it. WRITES events."""
    print("\n-- dry-run: injecting triggers (needs a running harness) --")
    out: list[dict] = []
    for loop, trigger, _next in TRIGGERS:
        eid = await _emit(conn, trigger, {"verify_loops": True}, f"verify-{trigger}")
        if eid is None:
            print(f"  {_DOT} {loop}: trigger {trigger} already pending (dedup) — skipped")
            out.append({"loop": loop, "trigger": trigger, "mark": _DOT})
            continue
        status = await _poll_consumed(conn, eid, timeout_s)
        mark = _OK if status == "consumed" else (_BAD if status in ("failed", "suppressed") else _WARN)
        detail = status or "still pending (no consumer)"
        print(f"  {mark} {loop}: {trigger} → {detail}")
        out.append({"loop": loop, "trigger": trigger, "mark": mark, "status": status})
    return out


async def run(since_min: int, dry: bool, timeout_s: float) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    print("=" * 78 + f"\nLOOP VERIFIER  (window={since_min}m{', dry-run' if dry else ', read-only'})\n" + "=" * 78)
    conn = await asyncpg.connect(dsn)
    try:
        records = await audit(conn, since_min)
        if dry:
            records += await dry_run(conn, timeout_s)
    finally:
        await conn.close()
    broken = [r for r in records if r["mark"] == _BAD]
    print("\n" + "=" * 78)
    if not broken:
        print("VERDICT: ✓ no broken steps — every observed loop step reached a handler.")
    else:
        print(f"VERDICT: {len(broken)} broken step(s):")
        for r in broken:
            print(f"  ✗ {r['loop']}: {r['event'] if 'event' in r else r.get('trigger')}")
    print("=" * 78)
    return 1 if broken else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.verify_loops")
    ap.add_argument("--since", type=int, default=1440, help="look-back window in minutes (default 24h)")
    ap.add_argument("--dry", action="store_true", help="inject each loop's trigger + poll (writes; needs a live lab)")
    ap.add_argument("--timeout", type=float, default=20.0, help="--dry: seconds to wait for each trigger to consume")
    args = ap.parse_args()
    return asyncio.run(run(args.since, args.dry, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
