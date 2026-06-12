"""
Closure auditor — one read-only sweep for every place the lab produced work but the
loop never closed.

In an event-driven lab the silent failure mode is non-closure: something is emitted or
transitioned to a produced state, but the consumer that should ADVANCE it never runs —
an event whose handler is unregistered in the current run mode, a direction worked then
left mid-loop, a fetched corpus that never re-triggers its requester. `no_handler` was
treated as benign, so these dead-ends hid. This tool makes them all visible on demand;
the dispatcher's closure guard surfaces the same shapes continuously (loop.unclosed) and
auto-closes the research ladder.

    set -a; . ./.env; set +a
    python -m ops.closure_audit [--days 3]

Checks: event closure (non-telemetry events landing no_handler) · directions stuck
in-flight (approved+active, work all terminal, nothing open) · thin_corpus orphans
(acquired/queued but never advanced) · completed tasks whose claim never gathered
evidence · recent loop.unclosed indicators. Exit 1 if any open non-closure is found.

The telemetry/poll-consumed allowlist (CLOSURE_EXEMPT_EVENTS) is imported from
harness.dispatch so the auditor and the live guard can never drift apart.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from harness.dispatch import ACTIVE_CLAIM, CLOSURE_EXEMPT_EVENTS

_OK, _WARN, _BAD, _DOT = "✓", "⚠", "✗", "·"
_flags: list[str] = []


def _line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")
    if mark in (_WARN, _BAD):
        _flags.append(f"{mark} {text}")


def _h(title: str) -> None:
    print(f"\n{title}")


async def _event_closure(conn, days: int) -> None:
    _h("Event closure — emitted but never reaches a handler (no_handler, non-telemetry)")
    rows = await conn.fetch(
        "SELECT event_type, "
        "count(*) FILTER (WHERE status='consumed') AS consumed, "
        "count(*) FILTER (WHERE status='suppressed' AND suppression_reason='no_handler') AS nh, "
        "max(emitted_at) AS last "
        "FROM events WHERE emitted_at > now() - ($1||' days')::interval "
        "GROUP BY event_type HAVING count(*) FILTER (WHERE suppression_reason='no_handler') > 0 "
        "ORDER BY 3 DESC",
        str(days),
    )
    flagged = 0
    for r in rows:
        et = r["event_type"]
        if et in CLOSURE_EXEMPT_EVENTS:
            continue
        flagged += 1
        # consumed>0 in-window = it HAD a handler and is now being dropped (a regression) = ✗;
        # never consumed = an unwired event type = ⚠ (still a dead-end, lower certainty).
        mark = _BAD if r["consumed"] else _WARN
        was = " (was-handled — wiring regression)" if r["consumed"] else " (never handled)"
        _line(mark, f"{et}: {r['nh']} dropped no_handler{was}")
    if not flagged:
        _line(_OK, "every non-telemetry event reaches a handler")


async def _stuck_directions(conn) -> None:
    _h("Directions stuck in-flight (approved+active, work all terminal, nothing open → blocks deliberation)")
    rows = await conn.fetch(
        "SELECT c.id, c.status, "
        "count(t.*) AS tasks, "
        "count(t.*) FILTER (WHERE t.status IN ('pending','running')) AS open "
        "FROM claims c "
        "JOIN direction_gate dg ON dg.claim_id = c.id AND dg.status='approved' "
        "LEFT JOIN tasks t ON t.claim_id = c.id "
        "WHERE c.claim_kind='direction' AND c.status = ANY($1) "
        "GROUP BY c.id, c.status "
        "HAVING count(t.*) > 0 AND count(t.*) FILTER (WHERE t.status IN ('pending','running')) = 0 "
        "ORDER BY c.id",
        list(ACTIVE_CLAIM),
    )
    if not rows:
        _line(_OK, "no directions stuck in-flight")
    for r in rows:
        _line(_BAD, f"direction #{r['id']} ({r['status']}): {r['tasks']} task(s), 0 open — committed but not advancing")


async def _thin_corpus_orphans(conn) -> None:
    _h("Thin-corpus orphans (researched, corpus too thin, direction never advanced)")
    rows = await conn.fetch(
        "SELECT t.id, t.claim_id, c.status AS cstatus, c.last_evidence_at "
        "FROM tasks t JOIN claims c ON c.id = t.claim_id "
        "WHERE (t.result->>'disposition'='thin_corpus' OR t.result->>'blocker'='thin_corpus') "
        "AND c.status = ANY($1) AND c.last_evidence_at IS NULL "
        "ORDER BY t.id",
        list(ACTIVE_CLAIM),
    )
    if not rows:
        _line(_OK, "no thin-corpus orphans")
    for r in rows:
        _line(_WARN, f"task #{r['id']} → claim #{r['claim_id']} ({r['cstatus']}): thin_corpus, claim un-advanced")


async def _unadvanced_completed(conn) -> None:
    _h("Completed tasks whose claim never gathered evidence")
    n = await conn.fetchval(
        "SELECT count(DISTINCT t.claim_id) FROM tasks t JOIN claims c ON c.id = t.claim_id "
        "WHERE t.status='completed' AND c.last_evidence_at IS NULL AND c.status = ANY($1)",
        list(ACTIVE_CLAIM),
    )
    _line(_OK if not n else _WARN, f"active claims with a completed task but no evidence: {n}")


async def _eaten_events(conn, days: int) -> None:
    _h("Eaten events — loop-bearing events that terminated without their handler running")
    rows = await conn.fetch(
        "SELECT event_type, COALESCE(suppression_reason,'failed') AS why, count(*) AS n, max(emitted_at) AS last "
        "FROM events WHERE emitted_at > now() - ($1||' days')::interval "
        "AND (status='failed' OR (status='suppressed' AND suppression_reason <> 'no_handler')) "
        "AND NOT (event_type = ANY($2)) "
        "GROUP BY 1, 2 ORDER BY 3 DESC",
        str(days),
        list(CLOSURE_EXEMPT_EVENTS),
    )
    if not rows:
        _line(_OK, "no loop-bearing event was failed or gate-suppressed in the window")
    for r in rows:
        # failed = a handler crashed/timed out (terminal — nothing retries event rows);
        # everything else is a gate (dial/cost/slop/cooldown/manual) eating the trace.
        _line(_BAD if r["why"] == "failed" else _WARN, f"{r['event_type']} [{r['why']}]: {r['n']}  (last {r['last']})")


async def _indicators(conn, days: int) -> None:
    _h("loop.unclosed indicators emitted by the live guard")
    rows = await conn.fetch(
        "SELECT payload->>'kind' AS kind, count(*) AS n, max(emitted_at) AS last FROM events "
        "WHERE event_type='loop.unclosed' AND emitted_at > now() - ($1||' days')::interval "
        "GROUP BY payload->>'kind' ORDER BY 2 DESC",
        str(days),
    )
    if not rows:
        _line(_OK, "no loop.unclosed indicators from the guard")
    for r in rows:
        _line(_WARN, f"loop.unclosed [{r['kind']}]: {r['n']}  (last {r['last']})")


async def run(days: int) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    print("=" * 78 + f"\nCLOSURE AUDIT  (read-only; window={days}d)\n" + "=" * 78)
    conn = await asyncpg.connect(dsn)
    try:
        for check in (
            _event_closure,
            _eaten_events,
            _stuck_directions,
            _thin_corpus_orphans,
            _unadvanced_completed,
            _indicators,
        ):
            try:
                await (check(conn, days) if check in (_event_closure, _eaten_events, _indicators) else check(conn))
            except Exception as e:  # noqa: BLE001 — one check failing must not sink the report
                _line(_BAD, f"{check.__name__} check errored: {str(e)[:120]}")
    finally:
        await conn.close()

    print("\n" + "=" * 78)
    if not _flags:
        print("VERDICT: ✓ no open non-closures — every loop that ran also closed.")
    else:
        print(f"VERDICT: {len(_flags)} non-closure(s) to look at:")
        for f in _flags:
            print(f"  {f}")
        print("\nThe dispatcher's closure guard auto-closes the research ladder; ✗ event regressions need wiring.")
    print("=" * 78)
    return 1 if any(f.startswith(_BAD) for f in _flags) else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.closure_audit")
    ap.add_argument("--days", type=int, default=3, help="look-back window")
    return asyncio.run(run(ap.parse_args().days))


if __name__ == "__main__":
    sys.exit(main())
