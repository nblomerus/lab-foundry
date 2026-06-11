"""Unit tests for the first-party (lab output) trust gate — `_is_reproducible`.

A lab experiment/dataset certifies its OWN output to `user_asserted` only when it's
reproducible; otherwise it's quarantined (staged, auditable, not queryable). The
reproducibility predicate is pure, so these need no DB/network.
"""

from __future__ import annotations

from library.ingest.first_party import _is_reproducible


def test_lab_experiment_reproducible_with_full_triple():
    prov = {"image": "labfoundry-experiment:py311", "seed": 7, "code_hash": "abc123"}
    assert _is_reproducible("lab_experiment", prov) is True


def test_lab_experiment_seed_zero_is_valid():
    # seed=0 is a perfectly valid seed but falsy — the gate must use `is not None`,
    # not truthiness, or it wrongly quarantines a fully reproducible run.
    prov = {"image": "img", "seed": 0, "code_hash": "h"}
    assert _is_reproducible("lab_experiment", prov) is True


def test_lab_experiment_missing_field_quarantines():
    assert _is_reproducible("lab_experiment", {"image": "img", "seed": 1}) is False  # no code_hash
    assert _is_reproducible("lab_experiment", {"seed": 1, "code_hash": "h"}) is False  # no image
    assert _is_reproducible("lab_experiment", None) is False
    assert _is_reproducible("lab_experiment", {}) is False


def test_lab_dataset_needs_content_hash():
    assert _is_reproducible("lab_dataset", {"sha256": "deadbeef"}) is True
    assert _is_reproducible("lab_dataset", {"sha256": ""}) is False  # empty hash pins nothing
    assert _is_reproducible("lab_dataset", {"sha256": None}) is False
    assert _is_reproducible("lab_dataset", {"image": "img", "seed": 0}) is False  # no sha256
    assert _is_reproducible("lab_dataset", {}) is False


def test_unknown_source_kind_not_reproducible():
    assert _is_reproducible("arxiv", {"image": "x", "seed": 1, "code_hash": "h"}) is False
