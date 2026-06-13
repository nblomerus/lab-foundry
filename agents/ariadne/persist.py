"""
Persist Ariadne's direction tree — the advisory/active write path.

In SHADOW she writes nothing (ops.ariadne_firstlight). In ADVISORY/ACTIVE her output
becomes the agenda: a mission claim → direction claims (claim_kind, on claims.parent_id —
no new tree table, per the locked design) → per-direction claim_goals. Everything lands
as `status='proposed'` (the human/Planner review queue); advisory just means nothing
downstream acts until a human flips ariadne to active. History is preserved — each
deliberation writes a fresh mission+directions (the PI re-frames each round).
"""

from __future__ import annotations

import json
import logging
import os

from agents.ariadne.grade import _toks
from agents.ariadne.scoring import DIMENSIONS, composite, is_wellformed, priority_label
from agents.mimir.acquire import AcquireRequest as MimirAcquireRequest
from agents.mimir.acquire import request_acquire
from harness.loop_predicates import ACTIVE_SQL
from library.corpus.tools import corpus_search
from skills.client import LessonsClient

log = logging.getLogger(__name__)

# Trigram similarity at/above which a reflection lesson counts as a re-derivation of an existing one
# (so it REINFORCES that lesson instead of inserting a duplicate). 0.62 collapses punctuation/wording
# variants of the same insight while leaving genuinely distinct lessons apart (validated on the live set).
LESSON_DEDUP_THRESHOLD = float(os.environ.get("LESSON_DEDUP_THRESHOLD", "0.62"))


async def _coverage_score(text: str) -> int | None:
    """Real corpus coverage for a direction → a 1..5 evidence_availability grade, so the
    LLM's GUESS at "is there evidence to ground & test this" is grounded in what the Library
    can ACTUALLY support. Counts distinct on-topic certified docs (title shares ≥2 topic
    tokens) the corpus returns for the direction. Returns None when it can't assess (too few
    tokens / search error) — then the LLM's score stands. Best-effort: never blocks deliberation."""
    topic = _toks(text)
    if len(topic) < 2:
        return None
    try:
        chunks = await corpus_search(text, k=12, exclude_lab=True)
    except Exception:  # noqa: BLE001
        return None
    docs = {c.document_id for c in chunks if len(topic & _toks(c.title or "")) >= 2}
    n = len(docs)
    return 1 if n == 0 else 2 if n <= 1 else 3 if n <= 3 else 4 if n <= 6 else 5


async def request_evidence(state, requests) -> int:
    """Fire Ariadne's evidence requests into Mimir's acquire QUEUE — the demand side of the
    Library. Each AcquireRequest targets a SPECIFIC paper (an arxiv id when she knows it, else
    its exact title) she believes is missing — NOT a broad topic (the scouts already cover
    topics, so a topic query just returns already_have). Mimir adjudicates (cap → resolve →
    dedupe → trust-gated ingest). Best effort — a bad request must not fail the deliberation.
    Returns the count emitted."""
    n = 0
    for r in requests or []:
        why = r.why if len(r.why or "") >= 30 else f"{r.why or r.paper} — Ariadne wants this specific paper"
        try:
            # Prefer the exact arxiv id (a precise fetch); fall back to the exact title as a query.
            mreq = (
                MimirAcquireRequest(requester="pi", kind="paper", arxiv_id=r.arxiv_id, why=why)
                if r.arxiv_id
                else MimirAcquireRequest(requester="pi", kind="paper", query=r.paper, why=why)
            )
            await request_acquire(state, mreq)
            n += 1
        except Exception as e:  # noqa: BLE001 — demand side is best-effort
            log.warning("ariadne: acquire request failed for %r: %s", r.paper, e)
    if n:
        log.info("ariadne: queued %d specific-paper request(s) to Mimir", n)
    return n


async def persist_directions(state, out, *, run_id: int | None = None) -> dict:
    """Write mission → directions → claim_goals + decision scores (all 'proposed'). Returns counts.

    Supersedes the PRIOR active agenda first (one active mission at a time) so re-framings
    don't pile up across the continuous loop — old rows are invalidated, not deleted, so
    history is preserved and the move stays reversible."""
    async with state.pool.acquire() as conn, conn.transaction():
        # Capture the directions about to be superseded so each gets a transition audit row
        # (transition='supersede') — the bulk flip below stays as-is (a wholesale agenda
        # re-frame, NOT a per-claim invalidation: no per-direction claim.invalidated, no
        # findings-stale sweep), but its outcome is now queryable in direction_transitions.
        superseded_dirs = await conn.fetch(
            f"SELECT id, status::text AS st FROM claims WHERE claim_kind='direction' AND status IN {ACTIVE_SQL}"
        )
        superseded = await conn.execute(
            "UPDATE claims SET status='invalidated', invalidated_at=now(), "
            "invalidation_reason='superseded by a newer deliberation' "
            "WHERE claim_kind IN ('mission','direction') "
            "AND status IN ('proposed','tested','weakly_supported','replicated')"
        )
        for r in superseded_dirs:
            await conn.execute(
                "INSERT INTO direction_transitions "
                "(claim_id, from_status, to_status, transition, reason, decided_by, created_by_run_id) "
                "VALUES ($1, $2, 'invalidated', 'supersede', 'superseded by a newer deliberation', 'deliberate', $3)",
                r["id"],
                r["st"],
                run_id,
            )
        mission_id = await conn.fetchval(
            "INSERT INTO claims (statement, claim_kind, status, confidence, created_by_run_id) "
            "VALUES ($1, 'mission', 'proposed', 0.5, $2) RETURNING id",
            out.mission_frame[:4000],
            run_id,
        )
        n_dir = n_goal = n_scored = 0
        for d in out.directions:
            dir_id = await conn.fetchval(
                "INSERT INTO claims (statement, claim_kind, parent_id, status, confidence, created_by_run_id) "
                "VALUES ($1, 'direction', $2, 'proposed', 0.5, $3) RETURNING id",
                f"{d.title}: {d.statement}"[:4000],
                mission_id,
                run_id,
            )
            n_dir += 1
            if is_wellformed(d.scores):
                scores = {dim: getattr(d.scores, dim) for dim in DIMENSIONS}
                # Ground evidence_availability in REAL corpus coverage — the more conservative
                # of the LLM's guess and what the Library actually holds — so Ariadne stops
                # picking niches with no literature (the root cause of the thin_corpus churn).
                cov = await _coverage_score(f"{d.title} {d.statement}")
                if cov is not None:
                    scores["evidence_availability"] = min(scores["evidence_availability"], cov)
                comp = composite(scores)
                # Build the column list + placeholders FROM DIMENSIONS so adding a
                # dimension (e.g. impact) can never desync the columns from the values.
                _cols = ", ".join(DIMENSIONS)
                _vals = ", ".join(f"${i + 2}" for i in range(len(DIMENSIONS)))
                _n = len(DIMENSIONS)
                await conn.execute(
                    f"INSERT INTO direction_scores (claim_id, {_cols}, composite, priority, rationale) "
                    f"VALUES ($1, {_vals}, ${_n + 2}, ${_n + 3}, ${_n + 4})",
                    dir_id,
                    *[scores[dim] for dim in DIMENSIONS],
                    comp,
                    priority_label(comp),
                    d.scores.rationale,
                )
                n_scored += 1
            for g in d.claim_goals:
                await conn.execute(
                    "INSERT INTO claim_goals "
                    "(claim_id, expectation, kill_condition, novelty_target, "
                    "next_milestone, priority_hint, status, set_by_run_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'open', $7)",
                    dir_id,
                    g.expectation,
                    g.kill_condition,
                    g.novelty_target,
                    g.next_milestone,
                    g.priority_hint,
                    run_id,
                )
                n_goal += 1
    n_superseded = int(superseded.split()[-1]) if superseded and superseded.startswith("UPDATE") else 0
    log.info(
        "ariadne: superseded %d prior claim(s); persisted mission %s + %d directions + %d goals + %d scored",
        n_superseded,
        mission_id,
        n_dir,
        n_goal,
        n_scored,
    )
    return {
        "mission_id": mission_id,
        "directions": n_dir,
        "claim_goals": n_goal,
        "scored": n_scored,
        "superseded": n_superseded,
    }


async def persist_reflection(state, out, valid_ids, *, run_id: int | None = None) -> dict:
    """Apply a reflection: steer the standing directions + record strategic lessons.

    All writes are reversible (status flips, priority overrides, probationary lessons) and
    confined to Ariadne's precious-small tier:
      * retire       → claims.status='invalidated' (+ invalidation_reason)
      * reprioritize/pivot → direction_scores.priority := new_priority (scored directions only)
      * advance      → affirmation, no data change
      * lessons      → probationary rows in `lessons` (derived_via='reflection'), fed back
                       into future deliberation via recall_lessons.
    """
    vids = set(valid_ids)
    counts = {"retired": 0, "reprioritized": 0, "advanced": 0, "lessons": 0, "reinforced": 0}
    async with state.pool.acquire() as conn, conn.transaction():
        for v in out.verdicts:
            if v.claim_id not in vids:
                continue
            if v.assessment == "retire":
                # Single guarded write path: legal-edge check + audit row stamped transition='retire'
                # (queryable apart from a gap/supersede). Runs in this reflection transaction.
                adv = await state.advance_direction(
                    v.claim_id,
                    "invalidated",
                    transition="retire",
                    decided_by="reflect",
                    reason=v.reason,
                    run_id=run_id,
                    conn=conn,
                )
                if adv is not None:
                    counts["retired"] += 1
            elif v.assessment in ("reprioritize", "pivot") and v.new_priority:
                await conn.execute(
                    "UPDATE direction_scores SET priority=$2 WHERE claim_id=$1", v.claim_id, v.new_priority
                )
                counts["reprioritized"] += 1
            elif v.assessment == "advance":
                counts["advanced"] += 1

    # Lessons — DEDUP ON RE-DERIVATION. Reflection re-emits the same ~30 insights; a blind INSERT piled
    # up ~40% duplicates that crowded the recall window and a probationary lesson could NEVER graduate
    # (it bypasses the Curator/Router that records lesson_applications). Now a near-duplicate REINFORCES
    # the existing lesson with a synthetic supportive application — so re-derivation becomes promotion
    # pressure (reconcile_lessons promotes at >=5) instead of table spam — and a genuinely new lesson is
    # inserted probationary. Each insert auto-commits (no surrounding txn) so intra-run dupes also collapse.
    lessons = LessonsClient(pool=state.pool)
    async with state.pool.acquire() as conn:
        for les in out.lessons:
            text = les.lesson.strip()
            if not text:
                continue
            dup_id = await lessons.find_near_duplicate(
                "ariadne.deliberate", text[:2000], threshold=LESSON_DEDUP_THRESHOLD
            )
            if dup_id is not None:
                # Re-derived. Credit the original (promotion pressure) when we have a run to credit;
                # either way DON'T insert the duplicate. A missing run_id degrades to plain dedup.
                if run_id is not None:
                    await lessons.credit_recurrence(dup_id, run_id)
                    counts["reinforced"] += 1
                continue
            await conn.execute(
                "INSERT INTO lessons (applies_to_invocation, applies_when, lesson_text, rationale, "
                "derived_from_run_id, derived_via, status, confidence) "
                "VALUES ('ariadne.deliberate', $1, $2, $3, $4, 'reflection', 'probationary', 0.40)",
                json.dumps({"when": les.applies_when} if les.applies_when else {}),
                text[:2000],
                (les.rationale or "")[:2000],
                run_id,
            )
            counts["lessons"] += 1
    log.info("ariadne: reflection applied — %s", counts)
    return counts
