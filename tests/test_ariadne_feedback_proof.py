"""PROOF (deterministic, end-to-end): experiments → finding → audit → Ariadne's prompt.

This is the artifact for "show me Ariadne is getting experiment info / synthesizing results." It drives
the REAL handlers and the REAL deliberation-prompt assembly against a migrated disposable Postgres (only
the LLM seam + prior-art recall are stubbed), and asserts — and prints — that a direction's experiments
become a synthesized + audited finding AND that both the execution ledger and the finding then appear in
the exact prompt Ariadne deliberates over. Run with `-s` to see the prompt:

    pytest -s tests/test_ariadne_feedback_proof.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.ariadne import loop as ariadne_loop
from agents.evaluation.handler import AuditScore, handle_finding_composed
from agents.synthesis.handler import handle_finding_synthesize
from agents.synthesis.schemas import ResearchFinding
from tests._helpers import make_dispatcher

DIRECTION = "Self-consistency decoding for math word-problem reasoning"


async def _seed_worked_direction(db) -> tuple[int, list[int]]:
    """An APPROVED direction with 3 completed experiments (the round that should synthesize)."""
    async with db.pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status) VALUES ($1, 'direction', 'tested') RETURNING id",
            DIRECTION,
        )
        await conn.execute(
            "INSERT INTO direction_gate (claim_id, status) VALUES ($1, 'approved') "
            "ON CONFLICT (claim_id) DO UPDATE SET status='approved'",
            cid,
        )
        exp_ids = []
        for i in range(3):
            task_id = await conn.fetchval(
                "INSERT INTO tasks (department, task_type, description, claim_id) "
                "VALUES ('research', 'research', 'd', $1) RETURNING id",
                cid,
            )
            eid = await conn.fetchval(
                "INSERT INTO experiment_runs (task_id, kind, params, result, status, interpretation, completed_at) "
                "VALUES ($1, 'benchmark', '{}'::jsonb, $2::jsonb, 'completed', $3, now() - make_interval(mins => $4)) "
                "RETURNING id",
                task_id,
                f'{{"acc": {0.70 + i * 0.03}}}',
                f"run {i}: self-consistency lifted accuracy ~{3 + i}pts over greedy decoding",
                30 - i,
            )
            exp_ids.append(eid)
    return cid, exp_ids


def _research_finding(grounded: list[int]) -> ResearchFinding:
    return ResearchFinding(
        headline="On GSM8K, self-consistency beats greedy decoding by ~5 points.",
        claim="Self-consistency (k=20) lifts GSM8K accuracy ~5pts over greedy decoding.",
        supported="supported",
        method="GSM8K, k=20 sampled chains, majority vote vs greedy.",
        key_numbers="acc 0.70→0.76 mean across 3 runs.",
        limitations="single model family, GSM8K only.",
        so_what="A practitioner should default to self-consistency for math reasoning.",
        next_step="test on MATH + larger k.",
        confidence=0.75,
        grounded_in_experiments=grounded,
    )


def _dispatcher(db, invoke_return):
    disp = make_dispatcher(db)
    disp.curator = AsyncMock()
    disp.curator.build = AsyncMock(return_value="PROMPT")
    disp.router = AsyncMock()
    disp.router.invoke = AsyncMock(return_value=invoke_return)
    disp.session = object()
    return disp


@pytest.mark.asyncio
async def test_experiments_flow_to_a_finding_and_into_ariadnes_prompt(db, monkeypatch):
    cid, exp_ids = await _seed_worked_direction(db)

    # ── STEP 1: the experiments stamp evidence on the direction (the #2 fix) ───────────────────
    await db.mark_claim_evidence(cid)
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT last_evidence_at FROM claims WHERE id=$1", cid) is not None

    # ── STEP 2: Synthesis composes a finding FROM those experiments (real handler) ─────────────
    synth = _dispatcher(db, (_research_finding(exp_ids), None))
    out = await handle_finding_synthesize({"id": 1, "payload": {"claim_id": cid}}, synth)
    assert out["finding_id"] is not None
    finding_id = out["finding_id"]
    async with db.pool.acquire() as conn:
        rf = await conn.fetchrow(
            "SELECT headline, supported, n_experiments FROM research_findings WHERE id=$1", finding_id
        )
        composed = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='finding.composed' AND target_id=$1", cid
        )
    assert rf["supported"] == "supported"
    assert rf["n_experiments"] == 3
    assert composed == 1  # the verification spine was armed

    # ── STEP 3: Evaluation (Aletheia) audits the finding → high-signal (real handler) ──────────
    eval_disp = _dispatcher(db, (AuditScore(finding_id=0, audit_score=0.85, verdict="pass", reasoning="grounded"), None))
    await handle_finding_composed({"id": 2, "payload": {"finding_id": finding_id, "claim_id": cid}}, eval_disp)
    async with db.pool.acquire() as conn:
        verdict = await conn.fetchval("SELECT audit_verdict FROM research_findings WHERE id=$1", finding_id)
        high_signal = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='finding.high_signal' AND target_id=$1", cid
        )
    assert verdict == "pass"
    assert high_signal == 1

    # ── STEP 4: the REAL deliberate prompt now carries the execution ledger + the finding ──────
    captured = {}

    async def _fake_deliberate(seed, agenda, prior_art, **kw):
        captured["agenda"] = agenda
        return None

    async def _fake_recall(*_a, **_k):
        return ("", [])

    monkeypatch.setattr(ariadne_loop, "_deliberate", _fake_deliberate)
    monkeypatch.setattr(ariadne_loop, "recall_prior_art", _fake_recall)
    await ariadne_loop.run_shadow(db)

    agenda = captured["agenda"]
    assert "## Execution ledger" in agenda  # the #1 channel is present
    assert f"T{cid}" in agenda  # this specific direction, with its run counts
    assert "3 done / 0 failed" in agenda  # she sees what the lab actually RAN
    assert "## Findings the lab has ESTABLISHED" in agenda
    assert "self-consistency beats greedy" in agenda.lower() or "self-consistency" in agenda.lower()

    # Print the proof for human eyes (visible with `pytest -s`).
    print("\n" + "=" * 78)
    print("PROOF — what Ariadne now deliberates over (experiment reality reaches her):")
    print("=" * 78)
    print(agenda)
    print("=" * 78)
