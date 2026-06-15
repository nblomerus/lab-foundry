"""Runnable-affordance awareness for the grounded researcher.

The researcher DECIDES thin_corpus vs needs_experiment but was blind to what the lab can actually RUN
— the /data packs, /models zoo, and local LLM broker the experiment DESIGNER already advertises. That
blind spot made "is the Library missing this paper?" trivially yes for any empirical task, so it almost
always picked thin_corpus (live: 1051 thin_corpus : 52 needs_experiment). This module gives both halves
of the fix a shared, manifest-grounded source of truth:

- `affordance_brief()` — a TERSE "what the lab can run locally" block for the decision prompt (names +
  modality only, not the designer's load boilerplate, to protect the corpus-evidence budget).
- `runnable_target(text)` — the deterministic floor: a task naming a staged dataset/model is runnable
  HERE, so a thin_corpus disposition on it is escalated to needs_experiment regardless of corpus depth.

Both read the SAME manifests as the experiment designer (agents/experiments/sandbox + the LLM-broker
env), so the decider and the runner never disagree about what exists. Fails safe: nothing staged → no
affordance block and the floor never fires (today's behaviour). Gated by RESEARCHER_RUNNABLE_BACKSTOP.
"""

from __future__ import annotations

import json
import os
import re

from agents.experiments import sandbox

# Kill-switch — disable the deterministic floor instantly without a code change.
BACKSTOP_ON = os.environ.get("RESEARCHER_RUNNABLE_BACKSTOP", "on").lower() in {"on", "1", "true"}

_DEFAULT_LLM_MODELS = "mistral:7b-instruct-q4_K_M, qwen2.5:14b-instruct-q4_K_M, qwen2.5-coder:7b, nomic-embed-text"
# /data/<name> or /models/<name> references inside a task description.
_PATH_RE = re.compile(r"/(?:data|models)/([A-Za-z0-9][\w\-.]*)")


def _norm(s: str) -> str:
    return s.strip().lower().replace("-", "_")


def _broker_on() -> bool:
    return os.environ.get("EXPERIMENT_LLM_BROKER", "").lower() in {"on", "1", "true"}


def _zoo_manifest() -> list[dict]:
    d = os.environ.get("EXPERIMENT_MODELS_DIR", "")
    if not d:
        return []
    try:
        with open(os.path.join(d, "manifest.json")) as fh:
            return json.load(fh) or []
    except (OSError, ValueError):
        return []


def _dataset_stems() -> set[str]:
    """Tokens the dataset manifest exposes: the file-stem, its family (stem before first '_'), and name."""
    stems: set[str] = set()
    for d in sandbox.read_manifest():
        f = d.get("file") or ""
        stem = _norm(f.rsplit(".", 1)[0]) if f else ""
        if stem:
            stems.add(stem)
            stems.add(stem.split("_")[0])  # gsm8k_test → gsm8k
        name = _norm(d.get("name") or "")
        if name:
            stems.add(name)
    return {s for s in stems if s}


def _model_tokens() -> set[str]:
    """Bare local model names a task may name directly (Ollama broker + the /models zoo)."""
    toks: set[str] = set()
    if _broker_on():
        for m in os.environ.get("EXPERIMENT_LLM_MODELS", _DEFAULT_LLM_MODELS).split(","):
            m = m.strip()
            if m:
                toks.add(_norm(m))
                toks.add(_norm(m.split(":")[0]))  # mistral:7b-… → mistral
    for m in _zoo_manifest():
        leaf = _norm((m.get("path") or "").rsplit("/", 1)[-1])
        if leaf:
            toks.add(leaf)
    return {t for t in toks if t}


def runnable_target(text: str) -> str | None:
    """If `text` names a staged dataset/model, return the matched token; else None. The deterministic
    floor that escalates a thin_corpus disposition to needs_experiment — the task is runnable HERE
    regardless of corpus thinness. Fails safe to None when the backstop is off or nothing is staged."""
    if not BACKSTOP_ON or not text:
        return None
    stems = _dataset_stems()
    models = _model_tokens()
    if not stems and not models:
        return None
    # 1) explicit /data//models/ path references — exact stem first, then family fallback.
    for raw in _PATH_RE.findall(text):
        tok = _norm(raw.rsplit(".", 1)[0])
        if tok in stems:
            return tok
        fam = tok.split("_")[0]
        if fam and fam in stems:
            return fam
    # 2) a bare local model name (a model-only task with no /data path).
    low = _norm(text)
    for m in sorted(models, key=len, reverse=True):
        if m and m in low:
            return m
    return None


def affordance_brief(max_items: int = 14) -> str:
    """A terse 'what the lab can RUN locally' block for the decision prompt — names + modality only,
    NOT the designer's load boilerplate (a few hundred tokens, not ~1.5k). Empty when nothing is
    staged, so the prompt omits the section and never claims a runnability that doesn't exist."""
    parts: list[str] = []
    manifest = sandbox.read_manifest()
    if manifest:
        ds = ", ".join(
            f"/data/{d.get('file', '?')} [{d.get('modality', '?')}/{d.get('task_type', '?')}]"
            for d in manifest[:max_items]
        )
        parts.append(f"- Datasets (READ-ONLY at /data): {ds}")
    if _broker_on():
        models = os.environ.get("EXPERIMENT_LLM_MODELS", _DEFAULT_LLM_MODELS).strip()
        parts.append(f"- Local LLM broker (real model behaviour; logprobs/ECE computable): {models}")
    zoo = _zoo_manifest()
    if zoo:
        ms = ", ".join(f"/models/{m.get('path', '?')}" for m in zoo[:max_items])
        parts.append(f"- Offline pretrained models (READ-ONLY at /models): {ms}")
    return "\n".join(parts)
