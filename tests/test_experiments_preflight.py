"""Unit tests for agents/experiments/preflight.py — the static gate run before a container slot.

check() catches the avoidable failures the live audit surfaced: banned/network/HF imports, hallucinated
flat-/data paths, and a missing result emit. The /data manifest is monkeypatched (no real pack needed).
"""

from __future__ import annotations

import pytest

from agents.experiments import preflight


@pytest.fixture(autouse=True)
def _flat_manifest(monkeypatch):
    # A small flat pack so the /data path check has something to validate against.
    manifest = [{"file": "adult.jsonl"}, {"file": "boolq_dev.jsonl"}]
    monkeypatch.setattr(preflight.sandbox, "read_manifest", lambda: manifest)
    monkeypatch.delenv("EXPERIMENT_MODELS_DIR", raising=False)  # zoo OFF → HF libs banned


def test_clean_code_passes():
    assert preflight.check("import numpy as np\nprint('{}')") == []


def test_empty_code_flagged():
    assert preflight.check("   ") == ["the experiment has no code"]


def test_syntax_error_flagged():
    out = preflight.check("def (:\n  pass")
    assert len(out) == 1 and out[0].startswith("SyntaxError")


def test_network_import_banned():
    out = preflight.check("import requests\nprint(1)")
    assert any("requests" in p and "no network" in p.lower() for p in out)


def test_urllib_and_socket_banned():
    out = preflight.check("import urllib.request, socket\nprint(1)")
    assert any("urllib" in p for p in out) and any("socket" in p for p in out)


def test_transformers_banned_when_zoo_off():
    out = preflight.check("import transformers\nprint(1)")
    assert any("transformers" in p and "not available" in p for p in out)


def test_transformers_allowed_when_zoo_present(monkeypatch, tmp_path):
    monkeypatch.setenv("EXPERIMENT_MODELS_DIR", str(tmp_path))  # zoo dir exists → HF libs allowed
    out = preflight.check("import transformers\nprint(1)")
    assert not any("transformers" in p for p in out)


def test_uninstalled_lib_flagged():
    out = preflight.check("import tensorflow as tf\nprint(1)")
    assert any("tensorflow" in p and "not installed" in p for p in out)


def test_hallucinated_data_subdir_flagged():
    out = preflight.check('rows = open("/data/boolq/boolq.jsonl")\nprint(1)')
    assert any("/data/boolq/boolq.jsonl" in p and "does not exist" in p for p in out)


def test_real_flat_data_path_ok():
    assert preflight.check('rows = open("/data/adult.jsonl")\nprint(1)') == []


def test_dynamic_data_path_not_flagged():
    # open("/data/" + name) — the regex shouldn't match an empty trailing segment
    out = preflight.check('import os\nopen("/data/" + os.environ.get("x", "adult.jsonl"))\nprint(1)')
    assert not any("/data/" in p for p in out)


def test_missing_result_emit_flagged():
    out = preflight.check("import numpy\nx = 1")
    assert any("never prints a result" in p for p in out)


def test_emit_helper_satisfies_result_check():
    code = 'import sys; sys.path.insert(0, "/opt/lab"); import exp\nexp.emit({"a": 1})'
    assert "never prints a result" not in " ".join(preflight.check(code))
