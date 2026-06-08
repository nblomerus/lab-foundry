"""
Ariadne's REFLECT & STEER pass — the feedback half of her operating loop (diagram
steps 6–8). Deliberation FRAMES a fresh tree; reflection STEERS the standing one.

She reads her standing directions + their decision scores + claim_goals + lifecycle
signals (status / confidence / last_evidence_at / age) AND the CURRENT field model
(which may have shifted since the directions were framed), then issues a verdict per
direction — advance | reprioritize | pivot | retire — and distils strategic lessons.

Lifecycle columns (confidence, last_evidence_at, invalidation_*) are the seams where
downstream results / critic verdicts land; today those producers are dormant, so the
live signal is the standing agenda × the refreshing landscape. WRITES NOTHING here —
persistence is agents.ariadne.persist.persist_reflection (advisory/active only).
"""

from __future__ import annotations

import logging

from agents.ariadne.loop import ARIADNE_MODEL, LAB_CONSTRAINTS, recall_lessons
from agents.ariadne.schemas import ReflectionOutput
from agents.llm import _chain_complete, _strip_fences
from agents.mimir.ask import answer_question
from library.graph.field_model import read_field_brief

log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("proposed", "tested", "weakly_supported", "replicated")

_REFLECT_SYSTEM = """You are Ariadne, Principal Investigator of an autonomous AI research lab,
performing a REFLECT & STEER pass over the STANDING research agenda (you are not framing a new
one). For each standing direction, weigh it against the CURRENT field model and its lifecycle:
- advance      — still sharp and well-placed; keep and let execution proceed.
- reprioritize — still valid but its priority should change (landscape or evidence shifted).
- pivot        — the core bet is worth keeping but the angle must change (e.g. its target area
                 saturated; re-aim at an adjacent EMERGING gap).
- retire       — the landscape moved against it (area now SATURATED/DECLINING), its kill-condition
                 is effectively met, or it is dominated by another direction.
Be decisive and grounded — cite the field-model trend or lifecycle signal in each reason. Distil
only GENERALIZABLE lessons (patterns that will recur), not one-offs."""

_REFLECT_SCHEMA_HINT = """Output JSON with exactly these keys:
{
 "portfolio_assessment": str,
 "verdicts": [ { "claim_id": int (an EXISTING standing direction id — never invent one),
   "assessment": "advance|reprioritize|pivot|retire", "reason": str,
   "new_priority": "high|medium|low"|null } ],
 "lessons": [ { "lesson": str, "rationale": str, "applies_when": str|null } ],
 "reprioritized_focus": str
}"""


def _age_days(created_at, now) -> int:
    try:
        return max(0, (now - created_at).days)
    except Exception:  # noqa: BLE001
        return 0


async def _standing_agenda(pool) -> tuple[str | None, list[int], str]:
    """(mission_statement, standing_direction_ids, formatted_agenda)."""
    async with pool.acquire() as conn:
        mission = await conn.fetchval(
            "SELECT statement FROM claims WHERE claim_kind = 'mission' "
            "AND status IN ('proposed','tested','weakly_supported','replicated') "
            "ORDER BY id DESC LIMIT 1"
        )
        rows = await conn.fetch(
            "SELECT c.id, c.statement, c.status, c.confidence, c.last_evidence_at, c.created_at, "
            "       ds.priority, ds.composite "
            "FROM claims c LEFT JOIN direction_scores ds ON ds.claim_id = c.id "
            f"WHERE c.claim_kind = 'direction' AND c.status IN {_ACTIVE_STATUSES} "
            "ORDER BY COALESCE(ds.composite, 0) DESC, c.id"
        )
        now = await conn.fetchval("SELECT now()")
        goals_by_dir: dict[int, list] = {}
        if rows:
            grows = await conn.fetch(
                "SELECT claim_id, expectation, kill_condition, status FROM claim_goals "
                "WHERE claim_id = ANY($1) ORDER BY claim_id",
                [r["id"] for r in rows],
            )
            for g in grows:
                goals_by_dir.setdefault(g["claim_id"], []).append(g)

    ids = [r["id"] for r in rows]
    lines = []
    for r in rows:
        pr = r["priority"] or "unscored"
        comp = f"{float(r['composite']):.2f}" if r["composite"] is not None else "—"
        ev = "no-evidence" if r["last_evidence_at"] is None else "has-evidence"
        lines.append(
            f"#{r['id']} [{r['status']} · conf={float(r['confidence']):.2f} · priority={pr} "
            f"(composite {comp}) · age={_age_days(r['created_at'], now)}d · {ev}]: {r['statement'][:240]}"
        )
        for g in goals_by_dir.get(r["id"], []):
            lines.append(f"     goal[{g['status']}]: expect={g['expectation'][:120]} || kill={g['kill_condition'][:120]}")
    return mission, ids, "\n".join(lines) if lines else "(no standing directions)"


async def _mimir_reflect_brief(state, mission: str | None, agenda: str, *, emit: bool) -> str:
    """CONVERSE with Mimir before steering: ask how the landscape has shifted AROUND the standing
    directions — what's now saturated vs still thin, and which fresh gaps should re-aim them. Same
    GraphRAG channel as deliberation (emits mimir.ask/answered when `emit`, so it shows live + in
    the conversation history). Best-effort — returns '' on failure. Read-only."""
    pool = getattr(state, "pool", None)
    if pool is None:
        return ""
    try:
        rows = await pool.fetch(
            "SELECT concept_name FROM field_model WHERE trend_state IN ('emerging','hot') "
            "ORDER BY (trend_state = 'emerging') DESC, total_papers DESC LIMIT 6"
        )
        anchors = ", ".join(r["concept_name"] for r in rows)
    except Exception:  # noqa: BLE001
        anchors = ""
    question = (
        f"Reflecting on the STANDING research agenda for the mission: {(mission or '(none)')[:300]}. "
        f"The current directions are:\n{agenda[:1200]}\n"
        f"{('Active/emerging areas right now: ' + anchors + '. ') if anchors else ''}"
        "For these directions, which of their areas are now well-covered/saturated versus still "
        "thin, how has the landscape shifted since they were framed, and what fresh under-explored "
        "gaps should re-aim or replace them?"
    )
    try:
        a = await answer_question(question, k=8, state=(state if emit else None), asker="ariadne")
    except Exception as e:  # noqa: BLE001 — conversation is best-effort grounding
        log.warning("reflection: Mimir conversation failed: %s", e)
        return ""
    block = f"## Mimir's reflection synthesis (multi-hop GraphRAG over the Library)\n{a.answer}"
    if a.gaps:
        block += "\nGAPS Mimir flags now (re-aim toward these, retire away from saturated areas):\n" + "\n".join(
            f"- {g}" for g in a.gaps
        )
    return block


async def _deliberate_reflection(
    mission: str, agenda: str, field_brief: str, lessons: str, mimir_block: str, *, model: str
) -> ReflectionOutput:
    user = (
        f"# Mission\n{mission or '(none set)'}\n\n"
        f"# Standing agenda (steer THESE — reference directions by their #id)\n{agenda}\n\n"
        f"# Current field model\n{field_brief or '(field model not built)'}\n\n"
        f"{mimir_block + chr(10) + chr(10) if mimir_block else ''}"
        f"# Lab capabilities & constraints (directions must fit this hardware)\n{LAB_CONSTRAINTS}\n\n"
        f"# {lessons or 'No standing lessons yet.'}\n\n"
        f"# Task\nReflect and steer the standing agenda, grounding your verdicts in MIMIR'S SYNTHESIS "
        f"above (how the landscape has shifted) and the field model. Any direction that needs training "
        f"large models or data-centre-scale compute (beyond the lab's constraints above) should be "
        f"PIVOTED to a lighter angle or RETIRED. {_REFLECT_SCHEMA_HINT}"
    )
    content = await _chain_complete(
        [{"role": "system", "content": _REFLECT_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.3,
        invocation_type="ariadne.reflect",
        step_name="reflect",
        primary_model=model,
    )
    return ReflectionOutput.model_validate_json(_strip_fences(content))


async def run_reflection(
    state, *, model: str = ARIADNE_MODEL, emit_conversation: bool = False
) -> tuple[ReflectionOutput | None, list[int]]:
    """Reflect over the standing agenda vs the current landscape — after CONVERSING with Mimir about
    how it has shifted. Returns (output, valid_ids). WRITES NOTHING to the corpus; emits the Mimir
    conversation only when `emit_conversation` (the live handler path). None when nothing to steer."""
    mission, ids, agenda = await _standing_agenda(state.pool)
    if not ids:
        return None, []
    field_brief = await read_field_brief(state.pool)
    lessons = await recall_lessons(state.pool)
    mimir_block = await _mimir_reflect_brief(state, mission, agenda, emit=emit_conversation)
    out = await _deliberate_reflection(mission, agenda, field_brief, lessons, mimir_block, model=model)
    return out, ids
