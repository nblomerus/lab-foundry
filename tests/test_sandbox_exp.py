"""Unit tests for agents/experiments/sandbox_exp.py — the mounted result helper (exp.emit).

emit() is the result contract (one compact JSON line on stdout) and must coerce numpy/torch
scalars + arrays so a result dict never dies on 'not JSON serializable'. Pure, no DB/sandbox.
"""

from __future__ import annotations

import contextlib
import io
import json

import numpy as np

from agents.experiments import sandbox_exp as E


def _emit(obj) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        E.emit(obj)
    return buf.getvalue().strip()


def test_emit_plain_dict_is_one_compact_line():
    out = _emit({"acc": 0.9, "n": 3})
    assert out == '{"acc": 0.9, "n": 3}'
    assert "\n" not in out


def test_emit_coerces_numpy_scalars_and_arrays():
    out = json.loads(_emit({"f": np.float64(0.5), "i": np.int64(7), "b": np.bool_(True), "arr": np.array([1, 2])}))
    assert out == {"f": 0.5, "i": 7, "b": True, "arr": [1, 2]}


def test_default_coerces_set_bytes_and_unknown():
    assert E._default({3, 1, 2}) == [1, 2, 3]  # all-scalar set → sorted list
    assert E._default(b"hi") == "hi"
    assert E._default(bytearray(b"yo")) == "yo"

    class Weird:
        def __repr__(self):
            return "<weird>"

    assert E._default(Weird()) == "<weird>"  # last-resort str()


def test_default_mixed_set_falls_back_to_list():
    out = E._default({(1, 2)})  # non-scalar element → list(), not sorted
    assert isinstance(out, list) and len(out) == 1
