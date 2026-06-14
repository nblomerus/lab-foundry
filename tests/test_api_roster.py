"""Tests for api/researcher.py roster endpoints — /researcher/roster + /researcher/roster/{id}.

The named-researcher roster + per-researcher drill-down (profile, owned directions, experiments,
status/failure breakdown). FastAPI TestClient over a mocked ScriptedPool.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from starlette.testclient import TestClient

from api import researcher
from tests._helpers import ScriptedPool

_NOW = datetime(2026, 6, 14, 12, 0)


def make_client(rules):
    app = FastAPI()
    app.include_router(researcher.router)
    app.state.pool = ScriptedPool(rules=rules)
    return TestClient(app)


def test_roster_lists_members_with_win_rate():
    rows = [
        {
            "id": 2,
            "name": "Hypatia",
            "persona": "bio",
            "specialty": "statistics-calibration",
            "status": "active",
            "owned_directions": 7,
            "done": 6,
            "failed": 3,
            "last_at": _NOW,
        },
        {
            "id": 1,
            "name": "Daedalus",
            "persona": "bio",
            "specialty": "systems-optimization",
            "status": "active",
            "owned_directions": 9,
            "done": 0,
            "failed": 0,
            "last_at": None,
        },
    ]
    body = make_client([("FROM researchers r ORDER BY r.id", rows)]).get("/researcher/roster").json()
    hyp, dae = body["researchers"]
    assert hyp["name"] == "Hypatia" and hyp["win_rate"] == 67.0 and hyp["last_at"] is not None
    assert dae["win_rate"] is None and dae["last_at"] is None  # no runs → no rate


def test_roster_detail_full():
    client = make_client(
        [
            (
                "FROM researchers WHERE id = $1",
                {
                    "id": 2,
                    "name": "Hypatia",
                    "persona": "bio",
                    "specialty": "statistics-calibration",
                    "status": "active",
                    "model": None,
                    "created_at": _NOW,
                },
            ),
            (
                "claim_kind = 'direction' ORDER BY c.id DESC",
                [
                    {
                        "id": 143,
                        "statement": "conformal prediction",
                        "status": "proposed",
                        "gate": "approved",
                        "confidence": 0.5,
                    }
                ],
            ),
            (
                "FROM experiment_runs e LEFT JOIN tasks t",
                [
                    {
                        "id": 50,
                        "status": "failed",
                        "data_realism": None,
                        "realism_mismatch": False,
                        "failure_class": "timeout",
                        "requires_gpu": True,
                        "params": {"hypothesis": "H1"},
                        "started_at": _NOW,
                        "completed_at": _NOW,
                        "claim_statement": "conformal prediction",
                    }
                ],
            ),
            ("GROUP BY status", [{"status": "completed", "n": 6}, {"status": "failed", "n": 3}]),
            ("GROUP BY fc", [{"fc": "timeout", "n": 2}]),
        ]
    )
    body = client.get("/researcher/roster/2").json()
    assert body["name"] == "Hypatia" and body["win_rate"] == 67.0
    assert body["directions"][0]["id"] == 143 and body["directions"][0]["gate"] == "approved"
    assert body["experiments"][0]["failure_class"] == "timeout" and body["experiments"][0]["hypothesis"] == "H1"
    assert body["by_failure_class"] == {"timeout": 2}


def test_roster_detail_not_found():
    client = make_client([("FROM researchers WHERE id = $1", [])])
    assert client.get("/researcher/roster/99").json() == {"error": "not found", "id": 99}
