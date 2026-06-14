"""Unit tests for the read/admin ops CLIs — ops/experiment_audit.py + ops/researchers.py.

These are thin DB-report/admin tools; the tests drive their async functions over a ScriptedConn/Pool
(universal row → every loop body runs; empty → the "(none)" branches). ops/build_model_zoo.py needs
the optional build-time `huggingface_hub` dep and is excluded from coverage (see pyproject omit).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ops import experiment_audit as EA
from ops import researchers as RS
from tests._helpers import ScriptedConn, ScriptedPool

pytestmark = pytest.mark.asyncio

# A row carrying every key any audit query reads, so each for-loop body executes once.
_AUDIT_ROW = {
    "status": "completed",
    "n": 5,
    "fc": "timeout",
    "dr": "real",
    "name": "Heron",
    "done": 3,
    "failed": 1,
    "infeasible": 0,
    "cap": "logprobs",
    "last": datetime(2026, 6, 14, 12, 0),
    "id": 42,
    "who": "Heron",
    "err": "boom error on /work/exp.py",
    "result": {"dataset": {"source": "fetch_openml('adult')"}},
    "code": "import numpy",
}


async def _run_audit(conn):
    await EA._overview(conn, 14)
    await EA._by_researcher(conn)
    await EA._capability_gaps(conn, 14)
    await EA._recent_failures(conn, 5)
    await EA._backfill_realism(conn)


async def test_experiment_audit_with_rows():
    await _run_audit(ScriptedConn(default_fetch=[_AUDIT_ROW]))


async def test_experiment_audit_empty_hits_none_branches():
    await _run_audit(ScriptedConn(default_fetch=[]))


async def test_backfill_realism_parses_json_string_result():
    conn = ScriptedConn(default_fetch=[{**_AUDIT_ROW, "result": '{"dataset": {"source": "make_classification"}}'}])
    await EA._backfill_realism(conn)
    assert any(c[0] == "execute" and "UPDATE experiment_runs SET data_realism" in c[1] for c in conn.calls)


_RES_ROW = {
    "id": 3,
    "name": "Heron",
    "specialty": "llm-retrieval-eval",
    "status": "active",
    "owned": 2,
    "done": 5,
    "failed": 3,
}


async def test_researchers_list_with_rows():
    await RS._list(ScriptedPool(default_fetch=[_RES_ROW]))


async def test_researchers_list_empty():
    await RS._list(ScriptedPool(default_fetch=[]))


async def test_researchers_add_upserts():
    pool = ScriptedPool(default_val=7)
    await RS._add(pool, "Archimedes", "systems-optimization", "loves a faster kernel", None)
    assert any("INSERT INTO researchers" in c[1] for c in pool.calls if c[0] == "fetchval")


async def test_researchers_set_status_found_and_missing():
    await RS._set_status(ScriptedPool(default_val=3), "Heron", "paused")  # found
    await RS._set_status(ScriptedPool(default_val=None), "Ghost", "paused")  # no such researcher


async def test_researchers_backfill_runs():
    await RS._backfill(ScriptedPool([("JOIN direction_gate g", [])]))
