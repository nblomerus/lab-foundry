"""Runnable-affordance floor + brief (agents/researcher/runnable.py).

Pure functions — no DB, no LLM. `sandbox.read_manifest` and the EXPERIMENT_* env are monkeypatched so
the staged-resource catalog is controlled. This is the deterministic, can't-regress half of the
thin_corpus→needs_experiment fix; the prompt half is an LLM judgment confirmed live.
"""

from __future__ import annotations

import pytest

from agents.researcher import runnable


@pytest.fixture
def stage(monkeypatch):
    """Stage a dataset manifest + LLM/model env for a test."""

    def _stage(datasets=None, *, broker=False, models="", models_dir=""):
        monkeypatch.setattr(runnable, "BACKSTOP_ON", True)
        monkeypatch.setattr(runnable.sandbox, "read_manifest", lambda: datasets or [])
        if broker:
            monkeypatch.setenv("EXPERIMENT_LLM_BROKER", "on")
        else:
            monkeypatch.delenv("EXPERIMENT_LLM_BROKER", raising=False)
        monkeypatch.setenv("EXPERIMENT_LLM_MODELS", models)
        monkeypatch.setenv("EXPERIMENT_MODELS_DIR", models_dir)

    return _stage


_ADULT = {"file": "adult.jsonl", "modality": "tabular", "task_type": "classification", "n": 1000}
_GSM8K = {"file": "gsm8k_test.jsonl", "modality": "text", "task_type": "qa", "n": 1319}


# ── runnable_target ──────────────────────────────────────────────────────────


def test_matches_explicit_data_path(stage):
    stage([_ADULT])
    assert runnable.runnable_target("Load adult income from /data/adult; train XGBoost, compute ECE") == "adult"


def test_family_fallback_for_subset_path(stage):
    stage([_GSM8K])  # /data/gsm8k matches the family of gsm8k_test
    assert runnable.runnable_target("Run self-consistency on /data/gsm8k, compute accuracy") == "gsm8k"


def test_phantom_path_not_in_manifest_is_none(stage):
    stage([_ADULT])  # corebench is not staged → no phantom run
    assert runnable.runnable_target("Select 50 tasks from /data/corebench") is None


def test_conceptual_task_with_no_runnable_token_is_none(stage):
    stage([_ADULT])
    assert runnable.runnable_target("Define what 'calibration' means and survey prior taxonomies") is None


def test_empty_manifest_and_no_models_is_none(stage):
    stage([])  # nothing staged → fails safe
    assert runnable.runnable_target("Load adult income from /data/adult") is None


def test_bare_local_model_name_matches(stage):
    stage([], broker=True, models="mistral:7b-instruct-q4_K_M, qwen2.5:14b-instruct-q4_K_M")
    # a model-only task with no /data path (e.g. the live T6078 shape)
    assert runnable.runnable_target("Run mistral on 100 random GSM8K problems and compute accuracy") == "mistral"


def test_backstop_off_returns_none(stage, monkeypatch):
    stage([_ADULT])
    monkeypatch.setattr(runnable, "BACKSTOP_ON", False)  # kill-switch
    assert runnable.runnable_target("Load /data/adult and train a model") is None


def test_empty_text_is_none(stage):
    stage([_ADULT])
    assert runnable.runnable_target("") is None


# ── affordance_brief ─────────────────────────────────────────────────────────


def test_brief_lists_datasets_and_broker(stage):
    stage([_ADULT, _GSM8K], broker=True, models="mistral:7b-instruct-q4_K_M")
    b = runnable.affordance_brief()
    assert "/data/adult.jsonl" in b
    assert "[tabular/classification]" in b
    assert "Local LLM broker" in b
    assert "mistral:7b-instruct-q4_K_M" in b


def test_brief_empty_when_nothing_staged(stage):
    stage([])  # no datasets, no broker, no models
    assert runnable.affordance_brief() == ""
