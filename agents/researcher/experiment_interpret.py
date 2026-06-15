"""The researcher INTERPRETS its own experiment — `experiment.completed`/`.failed`.

The interpret half of the lab's hands, moved under the researcher (migration 022): the OWNER reads the
result it authored, in its own voice, nudges the direction's confidence, ingests a first-party lab note
+ dataset card into the Library, and triggers cross-experiment synthesis once enough evidence lands.
Lives at agents.researcher.* so the dispatcher gates it on the `researcher` dial (agent_of). The
prompt builder + recipe remain the shared "design language" in agents.experiments.handler.
"""

from __future__ import annotations

import contextlib
import json
import logging

from agents.experiments import sandbox
from agents.experiments.handler import _classify_realism, _plan_wanted_real
from agents.experiments.schemas import ExperimentReport
from agents.researcher.identity import load_researcher, system_prompt
from agents.synthesis.handler import SYNTHESIS_MIN_EXPERIMENTS, SYNTHESIS_RESYNTH_STEP

log = logging.getLogger(__name__)


async def handle_experiment_completed(event: dict, dispatcher) -> dict | None:
    """`experiment.completed` → the owner interprets the result, nudges the direction, ingests a note."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    experiment_id = payload.get("experiment_id")
    claim_id = payload.get("claim_id")

    exp = await state.get_experiment(experiment_id) if experiment_id is not None else None
    if exp is None or not exp.get("result"):
        return {"skipped": True, "reason": "experiment missing or has no result", "experiment_id": experiment_id}

    params = exp.get("params") or {}
    hypothesis = params.get("hypothesis") or ""
    researcher = await load_researcher(state.pool, exp.get("researcher_id"))

    claim_statement = ""
    if claim_id is not None:
        try:
            claim = await state.get_claim(claim_id)
            claim_statement = claim.statement
        except ValueError:
            claim_statement = ""

    # Classify the run's data realism (static, no LLM) from its code + self-reported source, and flag a
    # plan-vs-actual mismatch — fed to the interpreter (discount synthetic) and persisted so synthesis can
    # discount synthetic-only findings and the A5 escalation can drive a real-data run.
    result_obj = exp["result"]
    if isinstance(result_obj, str):
        try:
            result_obj = json.loads(result_obj)
        except ValueError:
            result_obj = {}
    ds_obj = (result_obj.get("dataset") or result_obj.get("datasets") or {}) if isinstance(result_obj, dict) else {}
    ds_source = ds_obj.get("source", "") if isinstance(ds_obj, dict) else ""
    plan = (exp.get("provenance") or {}).get("dataset_plan") or params.get("dataset_plan") or ""
    realism = _classify_realism(exp.get("code") or "", ds_source)
    realism_mismatch = realism != "real" and _plan_wanted_real(plan, [d["name"] for d in sandbox.read_manifest()])

    prompt = await dispatcher.curator.build(
        invocation_type="experiments.interpret",
        context={
            "kind": exp.get("kind") or "code",
            "params": params,
            "result": exp["result"],
            "hypothesis": hypothesis,
            "claim_statement": claim_statement,
            "data_realism": realism,
            "realism_mismatch": realism_mismatch,
            "researcher_persona": system_prompt(researcher) if researcher else "",
        },
    )
    report, run_id = await dispatcher.router.invoke(
        prompt=prompt,
        output_schema_class=ExperimentReport,
        triggered_by_event_id=event["id"],
        session=dispatcher.session,
        step_name="experiments.interpret",
    )

    await state.set_experiment_interpretation(experiment_id, report.summary, run_id, report.narrative_note)
    await state.set_experiment_realism(experiment_id, realism, realism_mismatch)
    if realism_mismatch:
        log.warning("experiments: exp %s REALISM MISMATCH — plan wanted real data, run was %s", experiment_id, realism)

    # Move the direction's confidence — best-effort: an inactive claim can't be steered.
    conf_applied = None
    if claim_id is not None and report.confidence_delta:
        try:
            claim = await state.get_claim(claim_id)
            new_conf = max(0.0, min(1.0, claim.confidence + report.confidence_delta))
            await state.update_claim_confidence(
                claim_id,
                new_conf,
                reason=f"experiment {experiment_id}: {report.summary[:120]}",
                run_id=run_id,
            )
            conf_applied = [round(float(claim.confidence), 3), round(new_conf, 3)]
        except ValueError as e:  # claim not found or not active — the experiment still stands
            log.warning("experiments: confidence move skipped for claim %s: %s", claim_id, e)

    # An experiment landing on a direction IS evidence — stamp last_evidence_at so "has this been
    # worked" reflects experiments too (the literature path already does; interpretation did not).
    if claim_id is not None:
        with contextlib.suppress(Exception):  # evidence stamp is best-effort
            await state.mark_claim_evidence(claim_id)

    # Ingest a first-party lab note into the Library so Mimir / the corpus carry what the lab ran.
    text = _lab_note_markdown(experiment_id, claim_id, hypothesis, exp, report)
    await state.emit_corpus_event(
        "source.discovered",
        target_type="source",
        target_id=experiment_id,
        payload={
            "source": {
                "kind": "note",
                "source_kind": "lab_experiment",
                "canonical_key": f"exp:{experiment_id}",
                "title": f"Experiment {experiment_id}",
                "why": "first-party lab experiment",
            },
            "content": text,
            "provenance": exp.get("provenance") or {},
        },
        dedup_key=f"exp-doc-{experiment_id}",
    )

    # Capture the DATASET as its own first-party Library doc so the corpus carries how the data was
    # assembled (and how to regenerate it). The dataset's content sha256 goes into the card's provenance
    # so Mimir's lab_dataset trust gate can certify it (a hash present = the bytes are pinned).
    result = exp.get("result") or {}
    ds_fp = result.get("dataset") or result.get("datasets") or {}
    ds_sha = ds_fp.get("sha256") if isinstance(ds_fp, dict) else None
    dataset_provenance = {**(exp.get("provenance") or {}), "sha256": ds_sha}
    dataset_key = f"dataset:exp:{experiment_id}"
    await state.emit_corpus_event(
        "source.discovered",
        target_type="source",
        target_id=experiment_id,
        payload={
            "source": {
                "kind": "dataset",
                "source_kind": "lab_dataset",
                "canonical_key": dataset_key,
                "title": f"Dataset · experiment {experiment_id}",
                "why": "first-party lab experiment dataset",
            },
            "content": _lab_dataset_markdown(experiment_id, claim_id, exp),
            "provenance": dataset_provenance,
        },
        dedup_key=f"exp-dataset-{experiment_id}",
    )
    await state.set_experiment_dataset_refs(
        experiment_id,
        [
            {
                "canonical_key": dataset_key,
                "kind": "lab_dataset",
                "sha256": ds_sha,
                "plan": (exp.get("provenance") or {}).get("dataset_plan"),
                "fingerprint": ds_fp or None,
            }
        ],
    )

    # Condition-driven: once a direction has accumulated enough completed experiments, trigger the
    # terminal cross-experiment SYNTHESIS (the paper-shaped finding). Bucketed dedup so it fires once
    # per RESYNTH_STEP runs and re-fires when materially more evidence lands.
    synth_triggered = False
    if claim_id is not None:
        n_done = await state.count_completed_experiments_for_claim(claim_id)
        if n_done >= SYNTHESIS_MIN_EXPERIMENTS:
            bucket = n_done // max(1, SYNTHESIS_RESYNTH_STEP)
            await state.emit_corpus_event(
                "finding.synthesize",
                target_type="claim",
                target_id=claim_id,
                payload={"claim_id": claim_id, "experiment_count": n_done},
                dedup_key=f"synthesize-{claim_id}-{bucket}",
            )
            synth_triggered = True

    who = researcher.name if researcher else "researcher"
    log.info(
        "%s: interpreted exp %s (claim %s, supports=%s Δconf=%s, synth=%s)",
        who,
        experiment_id,
        claim_id,
        report.supports_direction,
        report.confidence_delta,
        synth_triggered,
    )
    return {
        "experiment_id": experiment_id,
        "claim_id": claim_id,
        "interpret_run_id": run_id,
        "supports_direction": report.supports_direction,
        "confidence": conf_applied,
        "ingested_note": True,
        "ingested_dataset": True,
        "synthesis_triggered": synth_triggered,
    }


async def handle_experiment_failed(event: dict, dispatcher) -> dict | None:
    """`experiment.failed` → record the failure as a researcher note. A killed/failed run is data too:
    the approach as written didn't produce a usable result. No confidence move."""
    state = dispatcher.state
    payload = event.get("payload") or {}
    experiment_id = payload.get("experiment_id")
    if experiment_id is None:
        return {"skipped": True, "reason": "no experiment_id in experiment.failed payload"}

    exp = await state.get_experiment(experiment_id)
    error = (exp.get("error") if exp else None) or "(no error recorded)"
    note = (
        f"Experiment failed: {error}. A failed/killed run is also data — the approach as written "
        "didn't produce a usable result."
    )
    await state.set_experiment_interpretation(experiment_id, None, None, note)
    log.info("experiments: recorded failure for exp %s: %s", experiment_id, error[:120])
    return {"experiment_id": experiment_id, "handled": True, "failed": True}


# -------------------------------------------------------------------------
# Lab-note / dataset-card markdown (the human-readable corpus record)
# -------------------------------------------------------------------------


def _provenance_line(provenance: dict) -> str:
    """One-line reproducibility stamp: seed + image digest + code hash."""
    return (
        f"seed={provenance.get('seed')} "
        f"image={provenance.get('image')}@{(provenance.get('image_digest') or '?')} "
        f"code_hash={provenance.get('code_hash')}"
    )


def _lab_note_markdown(experiment_id: int, claim_id, hypothesis: str, exp: dict, report: ExperimentReport) -> str:
    """A markdown lab note for the corpus — the human-readable record of one experiment."""
    params = exp.get("params") or {}
    provenance = exp.get("provenance") or {}
    result = exp.get("result")
    direction = f"T{claim_id}" if claim_id is not None else "(no direction)"
    return (
        f"## Experiment {experiment_id} on direction {direction}\n\n"
        f"**Hypothesis:** {hypothesis or '(none recorded)'}\n\n"
        f"**Dataset:** {provenance.get('dataset_plan') or '(synthesized in-code)'}\n\n"
        f"**Method / params:** `{json.dumps(params)[:600]}`\n"
        f"{_provenance_line(provenance)}\n\n"
        f"**Result:**\n```json\n{json.dumps(result, indent=2)[:2000]}\n```\n\n"
        f"**Interpretation:** {report.summary}\n\n"
        f"**Supports direction:** {report.supports_direction}  |  "
        f"Δconfidence: {report.confidence_delta}\n\n"
        f"**Researcher note:** {report.narrative_note}\n"
    )


def _lab_dataset_markdown(experiment_id: int, claim_id, exp: dict) -> str:
    """A markdown dataset card for the corpus — how the experiment's data was assembled, captured so
    the dataset is reproducible (regenerate from the code+seed+image digest) and discoverable."""
    provenance = exp.get("provenance") or {}
    result = exp.get("result") or {}
    plan = provenance.get("dataset_plan") or "Synthesized in the experiment script (seeded)."
    direction = f"T{claim_id}" if claim_id is not None else "(no direction)"
    ds = result.get("dataset") or result.get("datasets")
    ds_block = f"\n**Reported shape / fingerprint:**\n```json\n{json.dumps(ds, indent=2)[:1200]}\n```\n" if ds else ""
    return (
        f"## Dataset for experiment {experiment_id} (direction {direction})\n\n"
        f"**How it was assembled:** {plan}\n\n"
        f"**Reproducibility:** regenerate by running the experiment's recorded code at "
        f"{_provenance_line(provenance)} — the data is produced deterministically from the seed inside "
        f"the pinned image, so the same bytes recur.{ds_block}\n"
    )
