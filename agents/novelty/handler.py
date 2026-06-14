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
# A HIGH-impact direction is worth running even at modest novelty (validating/extending prior art
# under the lab's conditions IS decision-grade work). At/above this independent-impact bar, a
# non-redundant, impactful direction passes without also clearing the novelty floor.
ADJ_HIGH_IMPACT = int(os.environ.get("ADJUDICATE_HIGH_IMPACT", "4"))

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

- `novelty_independent` (1-5): how much does this ADD beyond the nearest prior art shown? This is an
  APPLIED lab on consumer hardware — VALIDATING or EXTENDING known work under new conditions (small/open
  models, out-of-distribution, tight compute, a real benchmark the prior work didn't use) is legitimately
  worth doing even when it is not paper-novel. Score what it ADDS; reserve 1-2 only for an exact re-run
  that contributes nothing new. Entries marked the lab's OWN output are NOT external prior art — NEVER let
  them lower this score; weigh them only by their outcome tag below.
- `impact_independent` (1-5): would a CLEAR answer change a real build/deploy decision a named practitioner
  faces? Score the decision value, not how interesting it sounds. A decision-grade question can be a 4-5
  even if its novelty is modest.
- `is_novel`: true if it adds something beyond the prior art shown — an extension/validation under new
  conditions COUNTS. false only for an exact re-run that adds nothing.
- `is_impactful`: true if you can name the concrete decision a clear answer changes.
- `redundant`: true ONLY if it re-asks a question a prior direction ANSWERED (status ANSWERED). A direction
  marked OPEN or ATTEMPTED BUT NOT ANSWERED settles nothing — the question is still open and a re-ask is
  unfinished business, not a rut. NEVER set redundant=true (or lower novelty) because of the lab's OWN
  directions unless one is explicitly marked ANSWERED. If redundant, name the direction in `redundant_note`.
- `rationale`: 2-3 sentences — the closest prior work, what this ADDS (or not), and the decision at stake.

Be fair, not gatekeeping. A HIGH-IMPACT question worth a real decision PASSES even at modest novelty.
HOLD only a re-ask of an ANSWERED question (redundant) or a direction no one would act on (not impactful).
An unanswered question stays fair game no matter how many times the lab has failed at it.
"""
    return PromptLayer(name="task_data", content=content, priority=1)


# -------------------------------------------------------------------------
# Recipe + route + system-prompt registration (idempotent — guard double-import)
# -------------------------------------------------------------------------

SYSTEM_PROMPTS.setdefault(
    "novelty",
    (
        "You are an independent prior-art reviewer for an APPLIED autonomous AI lab on consumer hardware. You judge "
        "whether a clear answer to a proposed direction would change a real build/deploy decision, and how much it "
        "adds beyond the nearest literature. For this lab, VALIDATING or EXTENDING prior work under new conditions "
        "(small/open models, OOD, tight compute, real benchmarks) is worth doing — judge decision value, not "
        "paper-publishable novelty. Flag a re-tread ONLY of ground the lab already ANSWERED; a failed or open attempt "
        "leaves its question open, so re-asking it is not a re-tread, and the lab's own outputs are never prior art "
        "that defeats novelty. You never see the proposer's own scores; your job is the external check they lack."
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
    """Derive pass/hold deterministically from the independent scores + flags (not LLM-set).

    HOLD only for the things that genuinely disqualify a direction for THIS applied lab: it re-asks
    an ANSWERED question (redundant), or no decision rides on it (not impactful / low impact). Novelty
    is NOT a hard veto — a direction that's impactful and non-redundant passes if EITHER it's genuinely
    novel OR it's high-impact (an extension/validation under the lab's conditions is worth running even
    if it doesn't clear the publishable-novelty bar). This breaks the single-axis novelty veto that was
    holding 67% of directions (incl. strong ones like #143)."""
    if adj.redundant or not adj.is_impactful or adj.impact_independent < ADJ_IMPACT_MIN:
        return "hold"
    novel_enough = adj.is_novel and adj.novelty_independent >= ADJ_NOVELTY_MIN
    high_impact = adj.impact_independent >= ADJ_HIGH_IMPACT
    return "pass" if (novel_enough or high_impact) else "hold"


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
