"""Agent identity admin — list / edit the named singleton agents (migration 024 `agent_identities`).

The named pantheon (Ariadne, Mimir, Themis, Metis, Calliope, Mnemosyne, Aletheia, Momus) the curator
resolves each agent's system persona from. The researcher roster lives in `ops.researchers`.

    set -a; . ./.env; set +a
    python -m ops.identities                                  # list the pantheon
    python -m ops.identities set-persona --agent novelty --persona "..."   # rewrite a persona
    python -m ops.identities rename --agent novelty --name Themis
    python -m ops.identities set-status --agent critic --status paused

Read-only by default (the bare list); the subcommands write.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from dotenv import load_dotenv


async def _list(pool) -> None:
    rows = await pool.fetch(
        "SELECT i.agent_name, i.name, i.role, i.status, m.mode "
        "FROM agent_identities i LEFT JOIN agent_modes m ON m.agent_name = i.agent_name "
        "ORDER BY i.agent_name"
    )
    if not rows:
        print("(no identities — run migration 024)")
        return
    print(f"{'agent':<14} {'name':<12} {'role':<26} {'status':<8} dial")
    for r in rows:
        print(f"{r['agent_name']:<14} {r['name']:<12} {(r['role'] or ''):<26} {r['status']:<8} {r['mode'] or '—'}")


async def _set_field(pool, agent: str, field: str, value: str) -> bool:
    n = await pool.fetchval(
        f"UPDATE agent_identities SET {field} = $1 WHERE agent_name = $2 RETURNING agent_name", value, agent
    )
    return bool(n)


async def _set_persona(pool, agent: str, persona: str) -> None:
    ok = await _set_field(pool, agent, "persona", persona)
    print(f"{agent}: persona updated" if ok else f"no identity for agent '{agent}'")


async def _rename(pool, agent: str, name: str) -> None:
    ok = await _set_field(pool, agent, "name", name)
    print(f"{agent}: name -> {name}" if ok else f"no identity for agent '{agent}'")


async def _set_status(pool, agent: str, status: str) -> None:
    ok = await _set_field(pool, agent, "status", status)
    print(f"{agent}: status -> {status}" if ok else f"no identity for agent '{agent}'")


async def _main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Agent identity admin")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    sp = sub.add_parser("set-persona")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--persona", required=True)
    rn = sub.add_parser("rename")
    rn.add_argument("--agent", required=True)
    rn.add_argument("--name", required=True)
    ss = sub.add_parser("set-status")
    ss.add_argument("--agent", required=True)
    ss.add_argument("--status", required=True, choices=["active", "paused", "retired"])
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        if args.cmd == "set-persona":
            await _set_persona(pool, args.agent, args.persona)
        elif args.cmd == "rename":
            await _rename(pool, args.agent, args.name)
        elif args.cmd == "set-status":
            await _set_status(pool, args.agent, args.status)
        else:
            await _list(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
