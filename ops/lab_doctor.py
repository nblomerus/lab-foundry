"""
Lab Doctor — one read-only health diagnostic for the running lab.

"Something's wrong with the lab — where?" This is the first thing to run. It does NOT
change anything; it inspects the event bus + agent_runs + tasks + Mimir and reports a
prioritized health picture with ✓ / ⚠ / ✗ markers and a final verdict. Drill deeper with
the /trace step-DAG (a session), Langfuse (an LLM call), or `ops.why` (causality).

    set -a; . ./.env; set +a
    python -m ops.lab_doctor [--hours 1]

Checks: activity/liveness · errors · stuck/orphaned runs · events not draining ·
stall/saturation/broken-agent indicators · unclosed-loop indicators · friction gates
(cooldown/cost-cap/slop) · cost today · Mimir intake + holds · per-agent last-seen.
Thresholds are deliberately loose — a ⚠ is a pointer, not a verdict. For the full
non-closure inventory run `python -m ops.closure_audit`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv

_OK, _WARN, _BAD, _DOT = "✓", "⚠", "✗", "·"
_flags: list[str] = []


def _line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")
    if mark in (_WARN, _BAD):
        _flags.append(f"{mark} {text}")


def _h(title: str) -> None:
    print(f"\n{title}")


async def _pulse(conn) -> None:
    """The lab's heartbeat — what it is doing RIGHT NOW and what it is waiting on.
    A pulse is emitted every watchdog tick (5 min); a stale one means the watchdog
    itself is down, which is the real 'lab looks dead'."""
    _h("NOW — latest lab.pulse (doing + waiting_on, emitted every watchdog tick)")
    row = await conn.fetchrow(
        "SELECT emitted_at, payload, extract(epoch FROM now() - emitted_at) AS age_s "
        "FROM events WHERE event_type = 'lab.pulse' ORDER BY id DESC LIMIT 1"
    )
    if row is None:
        _line(_WARN, "no lab.pulse yet — harness predates the heartbeat or never ticked")
        return
    p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    age = int(row["age_s"])
    stale = age > 15 * 60  # > 3 missed ticks ⇒ the watchdog is down — THIS is a dead lab
    _line(_BAD if stale else _OK, f"pulse age: {age // 60}m{age % 60:02d}s" + (" — WATCHDOG DOWN?" if stale else ""))
    for s in p.get("doing", []):
        _line(_DOT, f"doing:   {s}")
    for s in p.get("waiting_on", []):
        _line(_DOT, f"waiting: {s}")


async def _activity(conn, hours: int) -> None:
    _h("Activity / liveness")
    last_event = await conn.fetchval("SELECT max(emitted_at) FROM events")
    last_run = await conn.fetchval("SELECT max(started_at) FROM agent_runs")
    _line(_OK if last_event else _WARN, f"last event:     {last_event}")
    _line(_OK if last_run else _DOT, f"last agent run: {last_run}")
    rows = await conn.fetch(
        "SELECT status, count(*) FROM events WHERE emitted_at > now() - ($1||' hours')::interval "
        "GROUP BY status ORDER BY 2 DESC",
        str(hours),
    )
    _line(_DOT, f"events last {hours}h by status: " + (", ".join(f"{r['status']}={r['count']}" for r in rows) or "none"))
    n_runs = await conn.fetchval(
        "SELECT count(*) FROM agent_runs WHERE started_at > now() - ($1||' hours')::interval", str(hours)
    )
    _line(_DOT, f"agent runs last {hours}h: {n_runs}")


async def _errors(conn) -> None:
    _h("Errors (last 24h)")
    failed = await conn.fetch(
        "SELECT agent_name, count(*) AS n, max(left(error,80)) AS sample "
        "FROM agent_runs WHERE started_at > now() - interval '24 hours' "
        "AND (status = 'failed' OR error IS NOT NULL) GROUP BY agent_name ORDER BY 2 DESC LIMIT 8"
    )
    if not failed:
        _line(_OK, "no failed agent runs")
    for r in failed:
        _line(_WARN, f"{r['agent_name']}: {r['n']} failed — e.g. {r['sample']!r}")


async def _stuck(conn) -> None:
    _h("Stuck / orphaned (running too long → watchdog should reap, but flag it)")
    orphan_runs = await conn.fetchval(
        "SELECT count(*) FROM agent_runs WHERE status = 'running' AND started_at < now() - interval '30 minutes'"
    )
    _line(_OK if not orphan_runs else _WARN, f"agent_runs running >30m: {orphan_runs}")
    stuck_tasks = await conn.fetchval(
        "SELECT count(*) FROM tasks WHERE status = 'running' "
        "AND coalesce(started_at, created_at) < now() - interval '30 minutes'"
    )
    _line(_OK if not stuck_tasks else _WARN, f"tasks running >30m: {stuck_tasks}")
    pending_tasks = await conn.fetchval(
        "SELECT count(*) FROM tasks WHERE status = 'pending' AND created_at < now() - interval '30 minutes'"
    )
    _line(_OK if not pending_tasks else _WARN, f"tasks pending >30m (not picked up): {pending_tasks}")
    stale_pending = await conn.fetchval(
        "SELECT count(*) FROM events WHERE status = 'pending' AND emitted_at < now() - interval '5 minutes'"
    )
    _line(_OK if not stale_pending else _WARN, f"events pending >5m (not draining): {stale_pending}")


async def _stalls(conn, hours: int) -> None:
    _h("Stall / saturation / broken agents (in-process guards the watchdog's row-reap can't see)")
    rows = await conn.fetch(
        "SELECT event_type, count(*) AS n, max(emitted_at) AS last FROM events "
        "WHERE event_type IN ('agent.stalled','dispatch.saturated','agent.broken','agent.slow') "
        "AND emitted_at > now() - ($1||' hours')::interval "
        "GROUP BY event_type ORDER BY 2 DESC",
        str(hours),
    )
    if not rows:
        _line(_OK, "no stall / saturation / broken-agent indicators")
        return
    # stalled/saturated/broken are hard problems; slow is an early-warning ⚠.
    sev = {"agent.stalled": _BAD, "dispatch.saturated": _BAD, "agent.broken": _BAD, "agent.slow": _WARN}
    for r in rows:
        _line(sev.get(r["event_type"], _WARN), f"{r['event_type']}: {r['n']}  (last {r['last']})")


async def _closure(conn, hours: int) -> None:
    _h("Closure — work produced but the loop never closed (guard auto-closes the research ladder)")
    rows = await conn.fetch(
        "SELECT payload->>'kind' AS kind, count(*) AS n, max(emitted_at) AS last FROM events "
        "WHERE event_type = 'loop.unclosed' AND emitted_at > now() - ($1||' hours')::interval "
        "GROUP BY payload->>'kind' ORDER BY 2 DESC",
        str(hours),
    )
    if not rows:
        _line(_OK, "no unclosed-loop indicators")
        return
    # an unhandled event is a wiring regression (✗); stalled/gap directions are the ladder's work (⚠).
    sev = {"unhandled_event": _BAD}
    for r in rows:
        _line(sev.get(r["kind"], _WARN), f"loop.unclosed [{r['kind']}]: {r['n']}  (last {r['last']})")


async def _gates(conn, hours: int) -> None:
    _h("Friction gates (a choked gate looks like 'the lab stopped doing X')")
    rows = await conn.fetch(
        "SELECT suppression_reason, count(*) FROM events "
        "WHERE status = 'suppressed' AND emitted_at > now() - ($1||' hours')::interval "
        "AND suppression_reason IS NOT NULL GROUP BY suppression_reason ORDER BY 2 DESC",
        str(hours),
    )
    if not rows:
        _line(_OK, "no suppressed events")
    for r in rows:
        mark = _WARN if r["suppression_reason"] in ("cost_cap", "slop") else _DOT
        _line(mark, f"suppressed [{r['suppression_reason']}]: {r['count']}")


async def _cost(conn) -> None:
    _h("Cost (today)")
    total = (
        await conn.fetchval("SELECT coalesce(sum(cost_usd),0) FROM agent_runs WHERE started_at::date = current_date") or 0
    )
    _line(_WARN if total > 20 else _DOT, f"cost today: ${total:.2f}")
    by_tier = await conn.fetch(
        "SELECT model_tier, round(sum(cost_usd)::numeric,2) AS c FROM agent_runs "
        "WHERE started_at::date = current_date AND cost_usd > 0 GROUP BY model_tier ORDER BY 2 DESC"
    )
    if by_tier:
        _line(_DOT, "by tier: " + ", ".join(f"{r['model_tier']}=${r['c']}" for r in by_tier))


async def _mimir(conn, hours: int) -> None:
    _h("Mimir / substrate")
    ingested = await conn.fetchval(
        "SELECT count(*) FROM documents WHERE ingested_at > now() - ($1||' hours')::interval", str(hours)
    )
    quarantined = await conn.fetchval("SELECT count(*) FROM documents WHERE trust_state = 'quarantined'")
    _line(_DOT, f"docs ingested last {hours}h: {ingested}   |   quarantined total: {quarantined}")
    holds = await conn.fetchval(
        "SELECT count(*) FROM certifications WHERE decision = 'block' AND signals ? 'retraction_unverified'"
    )
    _line(_OK if not holds else _WARN, f"retraction-unverified holds (fail-closed): {holds}")
    corpus = await conn.fetchval("SELECT count(*) FROM documents WHERE queryable")
    _line(_DOT, f"queryable corpus: {corpus} docs")


async def _agents(conn) -> None:
    _h("Per-agent last-seen (a silent agent that should be active = a problem)")
    rows = await conn.fetch(
        "SELECT agent_name, max(started_at) AS last, "
        "count(*) FILTER (WHERE started_at > now() - interval '24 hours') AS d "
        "FROM agent_runs GROUP BY agent_name ORDER BY last DESC NULLS LAST LIMIT 15"
    )
    for r in rows:
        _line(_DOT, f"{r['agent_name']:<22} last={r['last']}  (24h runs={r['d']})")


async def _modes(conn) -> None:
    _h("Agent modes (off/shadow = paused · advisory/active = running)")
    try:
        rows = await conn.fetch("SELECT agent_name, mode, note FROM agent_modes ORDER BY agent_name")
    except Exception:  # noqa: BLE001 — table not migrated yet
        _line(_DOT, "agent_modes table not present (mode dial not migrated)")
        return
    if not rows:
        _line(_DOT, "no explicit modes — all agents on KNOWLEDGE_CORE_ONLY defaults")
        return
    for r in rows:
        mark = _WARN if r["mode"] in ("off", "shadow") else _DOT
        _line(mark, f"{r['agent_name']:<14} {r['mode']:<9} {('— ' + r['note']) if r['note'] else ''}")


async def run(hours: int) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    print("=" * 78 + f"\nLAB DOCTOR  (read-only; window={hours}h)\n" + "=" * 78)
    conn = await asyncpg.connect(dsn)
    try:
        for check in (_pulse, _activity, _errors, _stuck, _stalls, _closure, _gates, _cost, _mimir, _modes, _agents):
            try:
                await (check(conn, hours) if check in (_activity, _stalls, _closure, _gates, _mimir) else check(conn))
            except Exception as e:  # noqa: BLE001 — one check failing must not sink the report
                _line(_BAD, f"{check.__name__} check errored: {str(e)[:120]}")
    finally:
        await conn.close()

    print("\n" + "=" * 78)
    if not _flags:
        print("VERDICT: ✓ no anomalies surfaced.")
    else:
        print(f"VERDICT: {len(_flags)} thing(s) to look at:")
        for f in _flags:
            print(f"  {f}")
        print("\nNext: /trace a suspect session · Langfuse for the LLM call · `ops.why <event_id>` for causality.")
    print("=" * 78)
    return 1 if any(f.startswith(_BAD) for f in _flags) else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.lab_doctor")
    ap.add_argument("--hours", type=int, default=1, help="activity window")
    return asyncio.run(run(ap.parse_args().hours))


if __name__ == "__main__":
    sys.exit(main())
