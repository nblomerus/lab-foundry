"""
Lab debug console — freeze the lab, then probe agents with injected input.

The model: PAUSE freezes every agent (mode dial → off), so the lab stops acting on its
own. Then probe a specific agent with a crafted input and SEE its reasoning/verdict —
read-only, so you can poke freely without polluting state. RESUME unfreezes (restores
the prior modes). Watch the live cascade in the web UI (/events, /trace, localhost:8088).

    set -a; . ./.env; set +a
    python -m ops.lab_debug pause                       # freeze the lab
    python -m ops.lab_debug status                      # what's paused / running
    python -m ops.lab_debug ariadne --topic "agentic RAG for code"   # inject a request -> her tree
    python -m ops.lab_debug gate good                   # a clean arXiv source -> the trust verdict
    python -m ops.lab_debug gate bad                    # a retracted source -> quarantine
    python -m ops.lab_debug gate --url https://some-blog.example/sota
    python -m ops.lab_debug resume                      # unfreeze

The probes are READ-ONLY (Ariadne via run_shadow; the gate via classify_trust) — nothing
is written, so debug injections never touch the corpus, graph, or claims.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from agents.ariadne.grade import grade
from agents.ariadne.loop import run_shadow
from harness.agent_modes import _RESEARCH, get_agent_mode, set_agent_mode
from library.trust import DocMeta, classify_trust
from state.client import PostgresClient

KNOWN = sorted({"mimir"} | set(_RESEARCH))
STASH = Path(__file__).resolve().parents[1] / "backups" / ".debug_modes.json"


async def _pause(pool) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT agent_name, mode, note FROM agent_modes")
    STASH.parent.mkdir(exist_ok=True)
    STASH.write_text(json.dumps([dict(r) for r in rows]))
    for a in KNOWN:
        await set_agent_mode(pool, a, "off", "lab_debug pause")
    print(
        f"⏸  lab PAUSED — {len(KNOWN)} agents set off (prior modes stashed). "
        f"Probe with `ariadne`/`gate`; `resume` to unfreeze."
    )


async def _resume(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_modes")
    stash = json.loads(STASH.read_text()) if STASH.exists() else []
    for r in stash:
        await set_agent_mode(pool, r["agent_name"], r["mode"], r.get("note"))
    STASH.unlink(missing_ok=True)
    print(f"▶  lab RESUMED — restored {len(stash)} explicit mode(s); the rest back to defaults.")


async def _status(pool) -> None:
    print("agent modes (off/shadow=paused · advisory/active=running):")
    for a in KNOWN:
        m = await get_agent_mode(pool, a)
        print(f"  {a:<14} {m}")
    async with pool.acquire() as conn:
        recent = await conn.fetch(
            "SELECT event_type, status FROM events WHERE emitted_at > now() - interval '2 minutes' "
            "ORDER BY emitted_at DESC LIMIT 5"
        )
    print("recent events:", ", ".join(f"{r['event_type']}({r['status']})" for r in recent) or "none")


async def _ariadne(pool, topic: str | None) -> None:
    state = PostgresClient(pool=pool)
    print("→ injecting deliberation request" + (f" focused on: {topic!r}" if topic else "") + "  (read-only)\n")
    out = await run_shadow(state, focus=topic)
    print(f"MISSION\n  {out.mission_frame}\n")
    for i, d in enumerate(out.directions, 1):
        print(f"DIRECTION {i}: {d.title}\n  bet: {d.statement}\n  novelty: {d.novelty_rationale[:200]}")
        print(f"  grounded_in: {d.grounded_in}")
    r = await grade(out)
    print(
        f"\nGRADES: schema={r.schema_valid} goals={r.claim_goals_wellformed:.0%} "
        f"grounded={r.directions_grounded:.0%} citations_resolve={r.citations_resolved:.0%} "
        f"→ {'PASS' if r.passed else 'FAIL'}   (read-only — nothing written)"
    )


# Preset "sources" for the gate probe (signals are pre-resolved, as the ingest side does).
_GATE_PRESETS = {
    "good": dict(source_url="https://arxiv.org/abs/2406.00001", arxiv_id="2406.00001"),
    "bad": dict(source_url="https://arxiv.org/abs/2401.99999", arxiv_id="2401.99999", retracted=True),
    "spoof": dict(source_url="https://arxiv.org.evil.example/abs/1"),
    "peer": dict(source_url="https://www.nature.com/x", doi="10.1038/x", doi_resolves=True),
    "blocked": dict(source_url="https://github.com/o/r", license="all-rights-reserved"),
}


def _gate(args) -> None:
    if args.preset:
        meta = DocMeta(**_GATE_PRESETS[args.preset])
        label = args.preset
    else:
        meta = DocMeta(
            source_url=args.url,
            arxiv_id=args.arxiv,
            doi=args.doi,
            doi_resolves=args.doi_resolves,
            license=args.license,
            retracted=args.retracted,
        )
        label = "custom"
    tc = classify_trust(meta)
    verdict = "QUARANTINE/BLOCK" if (tc.blocked or tc.tier == "quarantined") else f"ADMIT @ {tc.tier}"
    print(f"→ trust gate on [{label}] {meta.source_url or meta.arxiv_id or meta.doi}")
    print(f"  verdict:   {verdict}")
    print(f"  tier:      {tc.tier}   blocked={tc.blocked}   needs_llm={tc.needs_llm}")
    print(f"  reason:    {tc.reason}")
    print(f"  signals:   {tc.signals}   (read-only — classify_trust, nothing ingested)")


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    if args.cmd == "gate":  # pure, no DB
        _gate(args)
        return 0
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        if args.cmd == "pause":
            await _pause(pool)
        elif args.cmd == "resume":
            await _resume(pool)
        elif args.cmd == "status":
            await _status(pool)
        elif args.cmd == "ariadne":
            await _ariadne(pool, args.topic)
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.lab_debug")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pause", help="freeze the lab (all agents off)")
    sub.add_parser("resume", help="restore prior modes")
    sub.add_parser("status", help="show modes + recent events")
    a = sub.add_parser("ariadne", help="inject a deliberation request (read-only)")
    a.add_argument("--topic", default=None, help="focus the deliberation on this topic")
    g = sub.add_parser("gate", help="probe Mimir's trust gate with a good/bad/custom source")
    g.add_argument("preset", nargs="?", choices=list(_GATE_PRESETS), help="good|bad|spoof|peer|blocked")
    g.add_argument("--url", default=None)
    g.add_argument("--arxiv", default=None)
    g.add_argument("--doi", default=None)
    g.add_argument("--doi-resolves", action="store_true", dest="doi_resolves")
    g.add_argument("--license", default=None)
    g.add_argument("--retracted", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
