"""
Build the lab's offline HF MODEL ZOO — the /models the experiment sandbox mounts.

The sandbox is --network none: experiments can reach local Ollama (the LLM broker) but had no
pretrained encoder/reranker models, so retrieval-reranking, NLI, and cross-encoder hypotheses were
forced to `infeasible` ("no cross-encoder available"). This curates a SMALL set of permissively
licensed models ONCE, on the host (build-time network), so they load OFFLINE from /models at run time:

    set -a; . ./.env; set +a
    pip install huggingface_hub        # build-time only; not needed in the harness env
    python -m ops.build_model_zoo [--dir /mnt/data/labfoundry-hf-models] [--force]

The experiment image installs transformers / sentence-transformers; the Quartermaster mounts this
dir read-only at /models and sets HF_HUB_OFFLINE=1, so a script loads by LOCAL PATH:

    from sentence_transformers import CrossEncoder
    ce = CrossEncoder("/models/ms-marco-MiniLM-L-6-v2")
    scores = ce.predict([(query, doc) for doc in docs])

Writes manifest.json (name, repo_id, task, path, license) — the design/debug prompt renders it
(_models_block) so the researcher knows exactly what /models holds. Idempotent: an already-staged
model is kept unless --force.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from huggingface_hub import snapshot_download

DEFAULT_DIR = os.environ.get("EXPERIMENT_MODELS_DIR", "/mnt/data/labfoundry-hf-models")

# Curated zoo — small, permissive, broadly useful for the lab's retrieval / NLI / embedding work.
# `path` is the local subdir under the zoo dir (and the /models/<path> the prompt advertises).
ZOO: list[dict] = [
    {
        "name": "ms-marco-MiniLM-L-6-v2",
        "repo_id": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "task": "cross-encoder reranker (query,passage -> relevance score)",
        "path": "ms-marco-MiniLM-L-6-v2",
        "license": "apache-2.0",
    },
    {
        "name": "nli-deberta-v3-small",
        "repo_id": "cross-encoder/nli-deberta-v3-small",
        "task": "NLI cross-encoder (premise,hypothesis -> contradiction/entailment/neutral)",
        "path": "nli-deberta-v3-small",
        "license": "apache-2.0",
    },
    {
        "name": "all-MiniLM-L6-v2",
        "repo_id": "sentence-transformers/all-MiniLM-L6-v2",
        "task": "sentence encoder (text -> 384-d embedding)",
        "path": "all-MiniLM-L6-v2",
        "license": "apache-2.0",
    },
]


def build(out_dir: str, *, force: bool) -> None:
    os.makedirs(out_dir, exist_ok=True)
    manifest: list[dict] = []
    for m in ZOO:
        dest = os.path.join(out_dir, m["path"])
        if os.path.isdir(dest) and os.listdir(dest) and not force:
            print(f"  · {m['name']}: already staged ({dest})")
        else:
            print(f"  ↓ {m['name']} <- {m['repo_id']}")
            snapshot_download(repo_id=m["repo_id"], local_dir=dest, local_dir_use_symlinks=False)
        manifest.append({k: m[k] for k in ("name", "repo_id", "task", "path", "license")})

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nstaged {len(manifest)} model(s) -> {out_dir}/manifest.json")
    print("rebuild the experiment image (make experiment-image) so transformers/sentence-transformers are present,")
    print("set EXPERIMENT_MODELS_DIR, and restart the harness so the QM mounts /models.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the offline HF model zoo (/models)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="output dir (default: EXPERIMENT_MODELS_DIR)")
    ap.add_argument("--force", action="store_true", help="re-download even if already staged")
    args = ap.parse_args()
    if not args.dir:
        print("no --dir / EXPERIMENT_MODELS_DIR set", file=sys.stderr)
        sys.exit(2)
    build(args.dir, force=args.force)


if __name__ == "__main__":
    main()
