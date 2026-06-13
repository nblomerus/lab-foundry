"""
The novelty agent — the independent adjudicator (the output-gate prior-art reviewer).

`direction.adjudicate` → for each scored, un-adjudicated direction: retrieve the ACTUAL
nearest prior art from the corpus + the lab's OWN recent directions, then one independent
LLM step (`novelty.adjudicate`) scores novelty + impact and flags rut-redundancy WITHOUT
seeing the proposer's self-scores. A deterministic pass/hold verdict is derived from those
fields and persisted; the gate (harness/ariadne_pace) requires verdict='pass'. The mode-dial
agent name is `novelty`.
"""

from __future__ import annotations

import logging
import os

from agents.novelty.schemas import DirectionAdjudication
from agents.synthesis.handler import SYNTHESIS_CONCLUDE_CONFIDENCE
from harness.curator import RECIPES, SYSTEM_PROMPTS, PromptLayer, Recipe
from harness.router import ROUTE, Tier
from library.corpus.tools import corpus_search

log = logging.getLogger(__name__)

# The independent floors a direction must clear to PASS (separate from the self-score floors
# the gate also checks). Tunable via env; default 3 mirrors the self-score gate floors.
ADJ_NOVELTY_MIN = int(os.environ.get("ADJUDICATE_NOVELTY_MIN", "3"))
ADJ_IMPACT_MIN = int(os.environ.get("ADJUDICATE_IMPACT_MIN", "3"))

_ACTIVE_DIRECTION = ("proposed", "tested", "weakly_supported", "replicated")


def _prior_outcome(d: dict) -> str:
    """One honest line on how a prior direction ENDED — the ground a redundancy verdict
    stands on. ANSWERED requires a concluded status or a decisive finding (same rule
    synthesis graduates on); a dead attempt without one left its question OPEN and must
    read that way, or the adjudicator holds re-asks of questions the lab never answered."""
    sup = d.get("finding_supported")
    conf = float(d.get("finding_confidence") or 0.0)
    decisive = sup is not None and sup != "inconclusive" and conf >= SYNTHESIS_CONCLUDE_CONFIDENCE
    if d.get("status") == "concluded" or decisive:
        detail = f"finding '{sup}' at confidence {conf:.2f}" if sup else "concluded"
        return f"ANSWERED ({detail})"
    if d.get("status") in _ACTIVE_DIRECTION:
        return f"OPEN — on the agenda now ({d.get('status')})"
    found = f"; best finding was '{sup}' at confidence {conf:.2f}" if sup else ", no decisive finding"
    return f"ATTEMPTED BUT NOT ANSWERED — {d.get('status')}{found}"


# -------------------------------------------------------------------------
# Curator task_data builder
# -------------------------------------------------------------------------


async def _build_adjudicate(ctx: dict, state, memory) -> PromptLayer:
    direction = ctx.get("direction_statement") or "(no statement)"
    prior_art = ctx.get("prior_art") or []
    prior_directions = ctx.get("prior_directions") or []
    pa = (
        "\n".join(
            f"- {p['title']}" + (" ← the lab's OWN output, not external prior art" if p.get("lab") else "")
            for p in prior_art
        )
        or "(no closely-related prior art retrieved)"
    )
    pd = "\n".join(f"- [{_prior_outcome(d)}] {d.get('statement')}" for d in prior_directions) or "(none)"

    content = f"""## Direction to adjudicate
{direction}

## Nearest prior art in the corpus (the closest existing work)
{pa}

## The lab's OWN recent directions, with how each one ENDED
{pd}

---

You are an independent, skeptical reviewer. You did NOT propose this direction and you do not see
the proposer's own scores — assess it on its merits against the evidence above.

- `novelty_independent` (1-5): does this clearly advance BEYOND the nearest prior art shown? Default LOW
  if the retrieved papers already answer it. Do not reward a re-skin of known work. Entries marked as
  the lab's OWN output are not external prior art — weigh them only by their outcome tag below.
- `impact_independent` (1-5): would a CLEAR answer change a real build/deploy decision a named practitioner
  faces? Score the decision value, not how interesting it sounds.
- `is_novel`: true ONLY if it genuinely goes beyond the prior art shown.
- `is_impactful`: true ONLY if you can name the concrete decision a clear answer changes.
- `redundant`: true ONLY if it re-asks a question a prior direction ANSWERED, or duplicates one marked
  OPEN on the agenda right now. A direction marked ATTEMPTED BUT NOT ANSWERED settles nothing — the lab
  tried and FAILED to answer it, so the question is still open and a re-ask is unfinished business, not
  a rut. Never hold a direction because a failed attempt "already covered" its topic. If redundant,
  name the direction in `redundant_note`.
- `rationale`: 2-3 sentences — the closest prior work, what's actually new (or not), and the decision at stake.

Be honest and demanding. A gap nobody would act on, or a re-ask of an ANSWERED question, should NOT
pass — but an unanswered question stays fair game no matter how many times the lab has failed at it.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe + route + system-prompt registration (idempotent — guard double-import)
# -------------------------------------------------------------------------

SYSTEM_PROMPTS.setdefault(
    "novelty",
    (
        "You are a tough, independent prior-art reviewer for an autonomous AI research lab. You judge whether a "
        "proposed research direction is genuinely novel against the actual nearest literature and whether a clear "
        "answer would change a real decision — and you flag re-treads of ground the lab already ANSWERED or is "
        "actively working. A failed attempt leaves its question open: re-asking it is not a re-tread. You default "
        "to skeptical: 'under-explored' is not 'worth doing', and a re-skin of known work is not novel. You never see "
        "the proposer's own scores; your job is the external check they lack."
    ),
)

if "novelty.adjudicate" not in RECIPES:
    RECIPES["novelty.adjudicate"] = Recipe(
        invocation_type="novelty.adjudicate",
        description="Independently score a proposed direction's novelty + impact against the nearest prior art.",
        agent="novelty",
        total_budget=6_000,
        use_cold_path=False,
        recall_sessions=[],
        recall_k=0,
        output_schema="DirectionAdjudication",
        task_data_builder=_build_adjudicate,
    )

ROUTE.setdefault("novelty.adjudicate", Tier.WORKHORSE)


def _verdict(adj: DirectionAdjudication) -> str:
    """Derive pass/hold deterministically from the independent scores + flags (not LLM-set)."""
    passes = (
        adj.novelty_independent >= ADJ_NOVELTY_MIN
        and adj.impact_independent >= ADJ_IMPACT_MIN
        and adj.is_novel
        and adj.is_impactful
        and not adj.redundant
    )
    return "pass" if passes else "hold"


# -------------------------------------------------------------------------
# Handler
# -------------------------------------------------------------------------


async def handle_direction_adjudicate(event: dict, dispatcher) -> dict | None:
    """`direction.adjudicate` → independently adjudicate every scored, un-adjudicated direction.
    With `reconsider_held` in the payload (the pacemaker's daily all-held re-look), HELD
    directions are re-adjudicated too — the UPSERT replaces their verdict, so a hold whose
    context has rotted (e.g. the "prior work" it leaned on was invalidated) can become a pass."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    directions = await state.get_unadjudicated_directions()
    reconsidered = 0
    if payload.get("reconsider_held"):
        seen_ids = {d["id"] for d in directions}
        held_dirs = [d for d in await state.get_held_directions() if d["id"] not in seen_ids]
        reconsidered = len(held_dirs)
        directions += held_dirs
        if held_dirs:
            log.info("novelty: reconsidering %d held direction(s) (pace all-held daily re-look)", reconsidered)
    if not directions:
        return {"adjudicated": 0, "reason": "nothing to adjudicate"}

    adjudicated = passed = held = 0
    for d in directions:
        statement = d["statement"]
        # The ACTUAL nearest prior art (external signal the self-score lacks) + the lab's own
        # recent directions WITH outcomes (the anti-rut signal that can also clear a re-ask).
        # Best-effort: a retrieval blip must not wedge it.
        # External-only prior art: exclude_lab keeps the lab's OWN proposals/findings out (its own
        # directions come through get_prior_directions_with_outcomes below). The whole block is
        # wrapped so a retrieval blip OR a chunk-shape change can never wedge the only independent
        # adjudicator — RetrievedChunk has no `source_kind`, so the `lab` flag reads source_url/tier.
        seen, prior_art = set(), []
        try:
            chunks = await corpus_search(statement, k=8, exclude_lab=True)
            for c in chunks:
                t = (c.title or "").strip()
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    is_lab = (c.source_url or "").startswith("lab://") or c.trust_tier == "user_asserted"
                    prior_art.append({"title": t, "lab": is_lab})
                if len(prior_art) >= 6:
                    break
        except Exception:  # noqa: BLE001
            pass
        prior_directions = await state.get_prior_directions_with_outcomes(exclude_claim_id=d["id"], limit=12)

        prompt = await dispatcher.curator.build(
            invocation_type="novelty.adjudicate",
            context={
                "direction_statement": statement,
                "prior_art": prior_art,
                "prior_directions": prior_directions,
            },
        )
        try:
            adj, run_id = await dispatcher.router.invoke(
                prompt=prompt,
                output_schema_class=DirectionAdjudication,
                triggered_by_event_id=event["id"],
                session=dispatcher.session,
                step_name="novelty.adjudicate",
            )
        except Exception:  # noqa: BLE001 — leave it un-adjudicated; the pacemaker re-emits next tick
            log.exception("novelty: adjudication failed for direction %s — will retry", d["id"])
            continue

        verdict = _verdict(adj)
        await state.persist_direction_adjudication(
            claim_id=d["id"],
            novelty_independent=adj.novelty_independent,
            impact_independent=adj.impact_independent,
            is_novel=adj.is_novel,
            is_impactful=adj.is_impactful,
            redundant=adj.redundant,
            redundant_note=adj.redundant_note,
            verdict=verdict,
            rationale=adj.rationale,
            nearest_prior_art=[p["title"] for p in prior_art],
            run_id=run_id,
        )
        adjudicated += 1
        passed += verdict == "pass"
        held += verdict == "hold"
        log.info(
            "novelty: adjudicated direction %s → %s (novelty=%d impact=%d novel=%s impactful=%s redundant=%s)",
            d["id"],
            verdict,
            adj.novelty_independent,
            adj.impact_independent,
            adj.is_novel,
            adj.is_impactful,
            adj.redundant,
        )

    out = {"adjudicated": adjudicated, "passed": passed, "held": held}
    if payload.get("reconsider_held"):
        out["reconsidered"] = reconsidered
    return out
