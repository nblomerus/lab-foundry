"""Pre-flight static checks for a designed experiment — catch the avoidable failures BEFORE a
container slot is spent (and before the QM's debug loop burns 5 attempts on them).

A live audit showed the dominant failures were not subtle: importing a library the image doesn't
have (`transformers`), reaching for the network directly (`urllib`/`requests` — the sandbox is
`--network none`), referencing a `/data` path that doesn't exist (`/data/boolq/boolq.jsonl` vs the
real `boolq_dev.jsonl`), or never printing a JSON result. All are detectable from the source with
`ast` + a manifest lookup. `check()` returns a list of human-readable problems (empty = clean); the
author's design handler feeds them to one cheap in-process debug rewrite before queueing.

Network libs are ALWAYS banned (there is no network). The HF stack (`transformers`/
`sentence_transformers`) is banned UNLESS the offline model zoo is staged (EXPERIMENT_MODELS_DIR
exists) — so this auto-relaxes when the zoo lands without a code change here.
"""

from __future__ import annotations

import ast
import os
import re

from agents.experiments import sandbox

# No network exists in the sandbox — these can only fail (ConnectionRefused) or signal intent to
# leave the box. Model calls go through the mounted /opt/lab/llm.py helper instead.
_NETWORK_BANNED = {"requests", "httpx", "aiohttp", "urllib", "urllib2", "socket", "http"}

# Heavy pretrained-model libs that are NOT in the base image. Allowed only when the offline HF model
# zoo is mounted (then load from /models, never an arbitrary hub name).
_HF_LIBS = {"transformers", "sentence_transformers", "datasets", "tokenizers", "accelerate"}

# Other libs that simply aren't installed (so an import is a guaranteed crash, not a capability).
_NOT_INSTALLED = {"tensorflow", "jax", "flax", "keras", "cv2", "PIL", "nltk", "spacy", "gensim"}

_DATA_PATH_RE = re.compile(r"""/data/([A-Za-z0-9_./-]+)""")


def _zoo_present() -> bool:
    d = os.environ.get("EXPERIMENT_MODELS_DIR", "")
    return bool(d) and os.path.isdir(d)


def _top_level_modules(tree: ast.AST) -> set[str]:
    """The root package of every import in the script (e.g. `import urllib.request` → 'urllib')."""
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def check(code: str) -> list[str]:
    """Static problems with `code` (empty list = clean). Cheap; runs before a container is spent."""
    problems: list[str] = []
    if not (code or "").strip():
        return ["the experiment has no code"]

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e.msg} (line {e.lineno})"]

    mods = _top_level_modules(tree)
    banned = _NETWORK_BANNED | _NOT_INSTALLED | (set() if _zoo_present() else _HF_LIBS)
    for m in sorted(mods & banned):
        if m in _NETWORK_BANNED:
            problems.append(
                f"imports `{m}` — the sandbox has NO network. For model calls use the mounted "
                f"/opt/lab/llm.py helper; never open a socket or fetch a URL."
            )
        elif m in _HF_LIBS:
            problems.append(
                f"imports `{m}` — not available. The base image has numpy/scipy/pandas/scikit-learn/"
                f"xgboost/statsmodels/torch only; use those or the /opt/lab/llm.py helper."
            )
        else:
            problems.append(f"imports `{m}` — not installed in the experiment image.")

    # /data path hallucination: the pack is FLAT (/data/<file>), so any literal /data/ reference whose
    # leading segment isn't a manifest filename is wrong (e.g. a hallucinated `/data/boolq/boolq.jsonl`
    # subdir vs the real flat `boolq_dev.jsonl`). Dynamic paths (open("/data/"+f)) don't match the regex.
    available = {d["file"] for d in sandbox.read_manifest()}
    if available:
        bad = set()
        for rest in _DATA_PATH_RE.findall(code):
            first = rest.split("/")[0]
            if first in available:
                continue
            if "." in first or "/" in rest:  # looks like a real filename or a (wrong) subdir path
                bad.add(rest)
        for r in sorted(bad):
            problems.append(
                f"reads `/data/{r}` which does not exist. The pack is flat — available /data files: "
                f"{', '.join(sorted(available))}."
            )

    # The run is scored on a JSON object printed to stdout — make sure the script emits one.
    if "print(" not in code and "emit(" not in code:
        problems.append("never prints a result — end with `emit(result)` (or `print(json.dumps(result))`).")

    return problems
