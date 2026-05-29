"""
External liveness watchdog for the autonomous loop.

Run by a systemd timer every few minutes, in its OWN process — that is the
whole point. The harness's internal watchdog can heal stale tasks, but if the
harness itself hangs (alive yet producing nothing) or dies, nothing inside it
can notice. This check lives outside the harness, reads the last activity
timestamp straight from Postgres, and if the loop has gone quiet past a
threshold while the company is unpaused and within its deadline, it:

  1. restarts the harness (systemctl --user restart boardroom-harness), and
  2. fires an optional webhook (ALERT_WEBHOOK_URL) so a human gets pinged.

Exit code 0 = healthy (or legitimately idle), 1 = stall detected & acted on.

Environment:
  DATABASE_URL              required
  LIVENESS_STALL_SECONDS    stall threshold, default 1200 (20 min)
  ALERT_WEBHOOK_URL         optional; POST the message body here (e.g. an
                            ntfy.sh topic URL, Slack/Discord webhook)
  LIVENESS_RESTART          "1" (default) to auto-restart; "0" to alert only
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone

import asyncpg
import httpx

STALL_SECONDS = int(os.environ.get("LIVENESS_STALL_SECONDS", "1200"))
WEBHOOK = os.environ.get("ALERT_WEBHOOK_URL")
RESTART = os.environ.get("LIVENESS_RESTART", "1") != "0"
HARNESS_UNIT = "boardroom-harness.service"


async def _last_activity(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            """
            SELECT
              GREATEST(
                (SELECT MAX(started_at) FROM agent_runs),
                (SELECT MAX(emitted_at) FROM events),
                (SELECT MAX(created_at) FROM findings)
              )                                              AS last_activity,
              (SELECT paused        FROM company_state WHERE id = 1) AS paused,
              (SELECT current_phase FROM company_state WHERE id = 1) AS phase,
              (SELECT deadline      FROM company_state WHERE id = 1) AS deadline
            """
        ))


def _restart_harness() -> None:
    subprocess.run(
        ["systemctl", "--user", "restart", HARNESS_UNIT],
        check=False,
    )


async def _fire_webhook(message: str) -> None:
    if not WEBHOOK:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(WEBHOOK, content=message.encode())
    except Exception as e:  # noqa: BLE001 — best-effort alert
        print(f"liveness: webhook failed: {e}", file=sys.stderr)


async def main() -> int:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        row = await _last_activity(pool)
    finally:
        await pool.close()

    # A paused company, or one past its deadline, is allowed to be quiet.
    if row.get("paused"):
        print("liveness: company paused — quiet is expected")
        return 0
    deadline = row.get("deadline")
    if deadline is not None and datetime.now(timezone.utc) > deadline:
        print("liveness: past deadline — quiet is expected")
        return 0

    last = row.get("last_activity")
    age = None if last is None else (datetime.now(timezone.utc) - last).total_seconds()

    if age is not None and age < STALL_SECONDS:
        print(f"liveness: healthy — last activity {int(age)}s ago (phase={row['phase']})")
        return 0

    age_str = "ever" if age is None else f"{int(age)}s"
    msg = (
        f"⚠ LabFoundry loop STALLED — no activity for {age_str} "
        f"(phase={row['phase']}, threshold={STALL_SECONDS}s). "
        + ("Restarting harness." if RESTART else "Restart disabled.")
    )
    print(msg, file=sys.stderr)
    if RESTART:
        _restart_harness()
    await _fire_webhook(msg)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
