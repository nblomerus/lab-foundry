"""Tests for api/quartermaster.py — the experiment ledger + detail + kill endpoints.

FastAPI TestClient over a mocked ScriptedPool (no real Postgres / Docker). Covers the researcher_name
+ failure_class surfacing, the JSON-string params path (_obj), the not-found branch, and the kill switch.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import FastAPI
from starlette.testclient import TestClient

from api import quartermaster
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 14, 12, 0)


def make_client(rules):
    app = FastAPI()
    app.include_router(quartermaster.router)
    app.state.pool = ScriptedPool(rules=rules)
    return TestClient(app)


def _list_row(**over):
    r = {
        "id": 1,
        "kind": "code",
        "status": "completed",
        "data_realism": "real",
        "realism_mismatch": False,
        "failure_class": None,
        "researcher_id": 2,
        "researcher_name": "Hypatia",
        "params": {"hypothesis": "H1: ECE improves", "claim_id": 3, "dataset_plan": "/data/adult.jsonl"},
        "resource_usage": {"iterations": 2},
        "researcher_notes": "a lab note",
        "interpretation": "the delta was +0.06",
        "error": None,
        "requires_gpu": False,
        "gpu_mem_mb": None,
        "priority": 6,
        "wall_clock_budget_s": 600,
        "mem_budget_mb": 2048,
        "kill_reason": None,
        "ingested_doc_id": None,
        "worker": "lf-exp-1",
        "started_at": _NOW,
        "completed_at": _NOW,
        "claim_statement": "the direction",
        "claim_confidence": 0.5,
    }
    r.update(over)
    return r


def _detail_row(**over):
    r = _list_row(**over)
    r.update(
        {
            "researcher_specialty": "statistics-calibration",
            "code": "import numpy\nprint('{}')",
            "result": {"acc": 0.9, "dataset": {"sha256": "abc"}},
            "provenance": {"seed": 7, "image": "labfoundry-experiment:py311"},
            "dataset_refs": [{"canonical_key": "dataset:exp:1"}],
        }
    )
    r.update(over)
    return r


def test_experiments_list_surfaces_researcher_and_failure_class():
    client = make_client(
        [
            ("GROUP BY status", [{"status": "completed", "n": 3}, {"status": "failed", "n": 1}]),
            ("FROM experiment_runs e ", [_list_row(id=1), _list_row(id=2, failure_class="timeout", status="failed")]),
            ("agent_name = 'quartermaster'", "active"),
        ]
    )
    body = client.get("/quartermaster/experiments?limit=5").json()
    assert body["mode"] == "active"
    assert body["by_status"] == {"completed": 3, "failed": 1}
    e0, e1 = body["experiments"]
    assert e0["researcher_name"] == "Hypatia" and e0["data_realism"] == "real" and e0["hypothesis"].startswith("H1")
    assert e1["failure_class"] == "timeout"


def test_experiments_list_parses_json_string_params():
    row = _list_row(params=json.dumps({"hypothesis": "H2", "claim_id": 9}), resource_usage=json.dumps({"iterations": 4}))
    client = make_client(
        [("GROUP BY status", []), ("FROM experiment_runs e ", [row]), ("agent_name = 'quartermaster'", None)]
    )
    e = client.get("/quartermaster/experiments").json()["experiments"][0]
    assert e["hypothesis"] == "H2" and e["iterations"] == 4


def test_experiment_detail_full_record():
    client = make_client([("WHERE e.id = $1", _detail_row(id=7))])
    body = client.get("/quartermaster/experiments/7").json()
    assert body["id"] == 7
    assert body["researcher_name"] == "Hypatia" and body["researcher_specialty"] == "statistics-calibration"
    assert body["failure_class"] is None and body["code"].startswith("import numpy")
    assert body["duration_s"] == 0.0  # started == completed


def test_experiment_detail_not_found():
    client = make_client([("WHERE e.id = $1", [])])
    assert client.get("/quartermaster/experiments/99").json() == {"error": "not found", "id": 99}


def test_kill_switch(monkeypatch):
    async def _fake_kill(eid):
        return None

    monkeypatch.setattr(quartermaster.sandbox, "kill", _fake_kill)
    client = make_client([])  # execute → default OK
    assert client.post("/quartermaster/experiments/7/kill").json() == {"killed": 7}
