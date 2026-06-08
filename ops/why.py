"""
ops.why — trace an item's JOURNEY across the whole pipeline (not one session's DAG).

The web /trace view shows ONE handler's step-DAG. This follows an item across MANY
sessions/agents — the cross-cutting causal chain. Because the deterministic path
doesn't create agent_runs (so events.emitted_by_run_id is null), the journey is
assembled from domain keys (document_id / canonical_key / certifications / events),
not generic event-walking.

    set -a; . ./.env; set +a
    python -m ops.why doc 45602          # a source's journey: scout → Mimir gate → Library
    python -m ops.why source 2406.12345  # same, by canonical key / arxiv id
    python -m ops.why event 129597        # raw event + its consuming run (where runs exist)

Read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv


def _t(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "  —  "


async def _doc_journey(conn, doc_id: int) -> int:
    d = await conn.fetchrow(
        "SELECT id, title, source_kind, canonical_key, trust_tier, trust_state, status, queryable, ingested_at "
        "FROM documents WHERE id = $1",
        doc_id,
    )
    if not d:
        print(f"no document #{doc_id}")
        return 1
    print("═" * 78)
    print(f"JOURNEY of document #{d['id']}  —  {(d['title'] or 'untitled')[:64]}")
    print(f"  source: {d['source_kind']}  key={d['canonical_key']}")
    print(
        f"  state:  {d['trust_tier']} · {d['trust_state']} · {d['status']} · "
        f"{'queryable ✓' if d['queryable'] else 'not queryable'}"
    )
    print("═" * 78)
    print("TIMELINE (scout → Library):")

    steps = []  # (sort_time, label, detail)
    seen = await conn.fetchrow(
        "SELECT first_seen_at, last_attempt_at, attempts FROM discovery_seen "
        "WHERE source_kind = $1 AND canonical_key = $2",
        d["source_kind"],
        d["canonical_key"],
    )
    if seen:
        steps.append(
            (
                seen["first_seen_at"],
                "SCOUT",
                f"{d['source_kind']} scout surfaced it (attempts={seen['attempts']})  [discovery_seen]",
            )
        )

    disc = await conn.fetch(
        "SELECT id, emitted_at, status FROM events WHERE event_type = 'source.discovered' "
        "AND payload->'source'->>'canonical_key' = $1 ORDER BY emitted_at LIMIT 3",
        d["canonical_key"],
    )
    for e in disc:
        steps.append((e["emitted_at"], "DISCOVERED", f"source.discovered (event #{e['id']}, {e['status']})"))

    certs = await conn.fetch(
        "SELECT id, decision, to_tier, used_llm, reasons, decided_by_run_id, created_at "
        "FROM certifications WHERE document_id = $1 ORDER BY created_at",
        doc_id,
    )
    for c in certs:
        verdict = "CERTIFY" if c["decision"] == "approve" else c["decision"].upper()
        trace = ""
        if c["decided_by_run_id"]:
            r = await conn.fetchrow("SELECT session_id, model_name FROM agent_runs WHERE id = $1", c["decided_by_run_id"])
            if r and r["session_id"]:
                trace = f"  → trace /trace/{r['session_id']} ({r['model_name']})"
        steps.append(
            (
                c["created_at"],
                f"MIMIR {verdict}",
                f'→ {c["to_tier"]}  used_llm={c["used_llm"]}  "{(c["reasons"] or "")[:48]}"  [cert #{c["id"]}]{trace}',
            )
        )

    for ev in await conn.fetch(
        "SELECT event_type, emitted_at, status FROM events "
        "WHERE target_type = 'document' AND target_id = $1 ORDER BY emitted_at",
        doc_id,
    ):
        label = {"document.parsed": "PARSE", "document.ingested": "INGEST", "mimir.ingest_blocked": "BLOCKED"}.get(
            ev["event_type"], ev["event_type"]
        )
        steps.append((ev["emitted_at"], label, ev["event_type"]))

    steps.sort(key=lambda s: (s[0] is None, s[0]))
    for t, label, detail in steps:
        print(f"  {_t(t)}  {label:<14} {detail}")
    if d["queryable"]:
        print(f"  {'─' * 8}  now retrievable in the Library (corpus_search) ✓")
    return 0


async def _event_chain(conn, event_id: int) -> int:
    e = await conn.fetchrow("SELECT * FROM events WHERE id = $1", event_id)
    if not e:
        print(f"no event #{event_id}")
        return 1
    print(
        f"EVENT #{e['id']}  {e['event_type']}  ({e['status']}, {_t(e['emitted_at'])})  "
        f"target={e['target_type']}#{e['target_id']}"
    )
    if e["emitted_by_run_id"]:
        r = await conn.fetchrow(
            "SELECT session_id, agent_name, invocation_type FROM agent_runs WHERE id = $1", e["emitted_by_run_id"]
        )
        print(
            f"  ← emitted by run #{e['emitted_by_run_id']} ({r['agent_name']}.{r['invocation_type']}, "
            f"trace /trace/{r['session_id']})"
        )
    else:
        print(
            "  ← emitted deterministically (no agent_run — generic causal link unavailable; "
            "use `why doc`/`source` for the domain journey)"
        )
    if e["consumed_run_id"] or e["consumed_by_handler"]:
        print(f"  → consumed by {e['consumed_by_handler']} (run #{e['consumed_run_id']})")
    elif e["status"] == "suppressed":
        print(f"  → suppressed ({e['suppression_reason']})")
    return 0


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn)
    try:
        if args.cmd == "doc":
            return await _doc_journey(conn, args.id)
        if args.cmd == "source":
            row = await conn.fetchrow(
                "SELECT id FROM documents WHERE canonical_key = $1 OR arxiv_id = $1 "
                "ORDER BY ingested_at DESC NULLS LAST LIMIT 1",
                args.key,
            )
            if not row:
                print(f"no document with key {args.key!r}")
                return 1
            return await _doc_journey(conn, row["id"])
        if args.cmd == "event":
            return await _event_chain(conn, args.id)
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.why")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doc", help="a document's journey: scout → Mimir → Library")
    d.add_argument("id", type=int)
    s = sub.add_parser("source", help="same, by canonical_key / arxiv_id")
    s.add_argument("key")
    ev = sub.add_parser("event", help="a single event + its run links")
    ev.add_argument("id", type=int)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
