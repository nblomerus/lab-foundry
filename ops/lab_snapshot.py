"""
Lab snapshot — back up the small, hard-to-rebuild state before any risky change.

The corpus chunks/embeddings (~19GB) and the context graph are RE-DERIVABLE (re-seed
~2h from /mnt/data/rag-bench-data; graph via ops.extract_concepts_backfill), so we don't
dump them by default. What's precious and small is the DECISIONS + REASONING: the
documents registry, Mimir's certifications, and claims/claim_goals (Ariadne's agenda).
Those snapshot in seconds — take one before touching anything.

    python -m ops.lab_snapshot              # reasoning snapshot (~MB) — the routine one
    python -m ops.lab_snapshot --full       # whole DB incl. chunks (~19GB) — rare, pre-destructive
    python -m ops.lab_snapshot --list

Creating a snapshot is READ-ONLY (pg_dump). Restoring is a DELIBERATE manual step (the
tool prints the command) — automated restore is exactly where the corpus-wipe risk lives,
so we don't automate it. For rolling back just Ariadne's writes, a surgical DELETE of her
proposed mission/direction claims is safer than any restore (her rows are isolated).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

PG = "labfoundry-postgres-1"
BK = Path(__file__).resolve().parents[1] / "backups"
# Small, hard-to-rebuild tables. NOT chunks (19GB, re-embeddable). documents (the
# registry) IS included so you know what to re-seed.
REASONING_TABLES = ["documents", "certifications", "claims", "claim_goals", "datasets", "agent_runs", "events"]


def _snapshot(full: bool) -> int:
    BK.mkdir(exist_ok=True)
    kind = "full" if full else "reasoning"
    out = BK / f"labfoundry_{kind}_{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
    cmd = ["docker", "exec", PG, "pg_dump", "-U", "labfoundry", "-d", "labfoundry"]
    if not full:
        for t in REASONING_TABLES:
            cmd += ["-t", t]
    print(f"snapshotting ({kind})… {out.name}")
    with out.open("wb") as f:
        dump = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        gz = subprocess.Popen(["gzip"], stdin=dump.stdout, stdout=f)
        dump.stdout.close()
        gz.communicate()
        rc = dump.wait() or gz.returncode
    if rc:
        out.unlink(missing_ok=True)
        print("✗ snapshot FAILED", file=sys.stderr)
        return 1
    mb = out.stat().st_size / 1e6
    print(f"✓ {out}  ({mb:.1f} MB)")
    print(f"  restore (CAREFUL — review first): gunzip -c {out} | docker exec -i {PG} psql -U labfoundry -d labfoundry")
    return 0


def _list() -> int:
    if not BK.exists():
        print("no backups/ yet")
        return 0
    snaps = sorted(BK.glob("labfoundry_*.sql.gz"))
    if not snaps:
        print("no snapshots")
    for f in snaps:
        print(f"  {f.name}  {f.stat().st_size / 1e6:.1f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.lab_snapshot")
    ap.add_argument("--full", action="store_true", help="dump the whole DB incl. chunks (~19GB)")
    ap.add_argument("--list", action="store_true", help="list existing snapshots")
    args = ap.parse_args()
    if args.list:
        return _list()
    return _snapshot(args.full)


if __name__ == "__main__":
    sys.exit(main())
