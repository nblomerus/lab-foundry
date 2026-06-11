"""
first_party.py — content-in-hand ingest for the lab's OWN outputs.

The external ingest path (library.ingest.pipeline.stage_source + Mimir's trust
gate) is built around *fetching* a source over the network and *judging* an
origin we don't control. First-party lab outputs are different on both axes: the
content is already in hand (an experiment's results write-up, a dataset's
provenance card), and trust is not a question of reputable-origin but of
REPRODUCIBILITY — a result we can re-run from a pinned image + seed + code hash
is something the lab itself certifies, not something it has to vet.

So this module is a thin, deterministic adapter onto the existing pipeline:

    ingest_first_party  ->  stage_source(content_text=...)   [skip the network]
                        ->  reproducible?
                              yes -> certify @ user_asserted + embed_and_finalize
                              no  -> quarantine (staged, unembedded, auditable)

It reuses the EXACT state methods Mimir uses for the trust write (set_document_trust
+ append_certification) and the same embed_and_finalize that flips a doc queryable,
so a first-party doc is indistinguishable from an external one once it lands —
only its tier (user_asserted) and the reproducibility signals on its certification
say where it came from. Every external call is wrapped: one failure logs and
returns None rather than raising into the harness.
"""

from __future__ import annotations

import logging

from library.ingest.pipeline import embed_and_finalize, stage_source

log = logging.getLogger(__name__)


def _is_reproducible(source_kind: str, provenance: dict | None) -> bool:
    """Decide whether a first-party output is reproducible enough to certify and
    make queryable, vs. quarantine.

    lab_experiment — needs the full re-run triple: a pinned container image, the
    RNG seed, and the code hash. With all three the lab can deterministically
    reproduce the result, so it certifies its own output.

    lab_dataset — needs a content hash (sha256) pinning the exact bytes, so the
    dataset a future reader queries is provably the one described.

    Anything missing a required field is NOT reproducible -> quarantine (staged,
    auditable, but not queryable until the provenance is filled in)."""
    if not provenance:
        return False
    if source_kind == "lab_experiment":
        # image + code_hash are non-empty strings; seed is checked `is not None`, not
        # by truthiness — seed=0 is a valid seed but falsy, and would otherwise wrongly
        # quarantine an experiment that is in fact fully reproducible.
        return bool(provenance.get("image")) and bool(provenance.get("code_hash")) and provenance.get("seed") is not None
    if source_kind == "lab_dataset":
        return bool(provenance.get("sha256"))  # a non-empty content hash pins the bytes
    if source_kind == "lab_finding":
        # a synthesis is reproducible iff it rests on the lab's OWN certified experiments —
        # the result it claims can be re-derived by re-running those runs (each itself pinned).
        return bool(provenance.get("grounded_in_experiments"))
    return False


async def ingest_first_party(
    state,
    *,
    kind: str,
    source_kind: str,
    canonical_key: str,
    title: str,
    content: str,
    provenance: dict | None,
) -> int | None:
    """Ingest one first-party lab output (experiment result / dataset) whose text
    is already in hand. Stages it via the normal pipeline (no network), then —
    keyed on REPRODUCIBILITY — either certifies it at the `user_asserted` tier and
    embeds it (queryable), or quarantines it (staged, unembedded, auditable).

    Returns the document id (existing on a dedupe, the new one on a fresh stage),
    or None if staging produced no document. Never raises into the caller: an
    external failure is logged and folded into a None / best-effort return."""
    desc = {
        "kind": kind,
        "source_kind": source_kind,
        "canonical_key": canonical_key,
        "url": None,
        "title": title,
        "why": "first-party lab output",
    }

    try:
        staged = await stage_source(desc, state, content_text=content)
    except Exception:  # noqa: BLE001 — one first-party source must never break the harness
        log.exception("first_party: stage_source failed for %s/%s", source_kind, canonical_key)
        return None

    doc_id = staged.get("document_id")
    if doc_id is None:
        # Quality gate rejected it / it chunked to nothing — nothing to certify.
        log.info("first_party: %s/%s not staged (%s)", source_kind, canonical_key, staged.get("reason"))
        return None
    if staged.get("deduped"):
        # Already in the corpus; return the existing id without re-certifying.
        log.info("first_party: %s/%s already ingested as doc %s", source_kind, canonical_key, doc_id)
        return doc_id

    reproducible = _is_reproducible(source_kind, provenance)
    signals = {
        "first_party": True,
        "source_kind": source_kind,
        "reproducible": reproducible,
        "provenance_keys": sorted(provenance.keys()) if provenance else [],
    }

    try:
        if reproducible:
            # Reproducible -> the lab certifies its own output at user_asserted and
            # makes it queryable. user_asserted is the dedicated first-party tier
            # (a verifiable identifier we *can* re-run, not a reputable external one).
            await state.set_document_trust(
                doc_id,
                tier="user_asserted",
                trust_state="provisional",
                status="certified",
            )
            await state.append_certification(
                doc_id,
                decision="approve",
                to_tier="user_asserted",
                to_state="provisional",
                signals=signals,
                used_llm=False,
                reasons="first-party lab output reproducible from pinned provenance — certified",
            )
            await embed_and_finalize(doc_id, state)
        else:
            # Not reproducible -> quarantine. Staged + auditable, but left unembedded
            # (non-queryable) until provenance is filled in. Mirrors Mimir's _block.
            await state.set_document_trust(
                doc_id,
                tier="quarantined",
                trust_state="quarantined",
                status="blocked",
            )
            await state.append_certification(
                doc_id,
                decision="block",
                to_tier="quarantined",
                to_state="quarantined",
                signals=signals,
                used_llm=False,
                reasons="first-party lab output lacks reproducible provenance — quarantined (not queryable)",
            )
    except Exception:  # noqa: BLE001 — trust/finalize failure must not raise into the harness
        log.exception("first_party: trust write/finalize failed for doc %s", doc_id)
        return None

    log.info(
        "first_party: doc %s ingested (%s, reproducible=%s)",
        doc_id,
        source_kind,
        reproducible,
    )
    return doc_id
