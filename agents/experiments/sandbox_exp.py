"""Lab helpers for experiment scripts — bind-mounted read-only into every container at /opt/lab/exp.py
(always present, even when the LLM broker is off). Stdlib only.

    import sys; sys.path.insert(0, "/opt/lab"); import exp
    exp.emit({"accuracy": acc, "delta": d, "dataset": {...}})

`emit(result)` IS the result contract: it prints exactly ONE compact JSON object as the LAST line of
stdout (what the sandbox scores) and coerces numpy / torch scalars + arrays (and sets/bytes), so a
result dict never dies on "Object of type ... is not JSON serializable" — the #1 avoidable failure.
"""

import json


def _default(o):
    """Best-effort coercion of common non-JSON-native values to a JSON form."""
    item = getattr(o, "item", None)  # numpy/torch 0-d scalar → python scalar
    if callable(item):
        try:
            return o.item()
        except Exception:  # noqa: BLE001 — fall through to other coercions
            pass
    tolist = getattr(o, "tolist", None)  # numpy/torch array → nested list
    if callable(tolist):
        try:
            return o.tolist()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(o, (set, frozenset)):
        return sorted(o) if all(isinstance(x, (int, float, str)) for x in o) else list(o)
    if isinstance(o, (bytes, bytearray)):
        return bytes(o).decode("utf-8", "replace")
    return str(o)


def emit(result) -> None:
    """Print `result` as the single, final JSON line the sandbox parses (numpy/torch-safe)."""
    print(json.dumps(result, default=_default))
