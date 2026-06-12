"""
Build the lab's offline BENCHMARK PACK — the /data the experiment sandbox mounts.

The sandbox is --network none: experiments can reach local models (the LLM broker) but
had no real evaluation data, so model-behaviour findings were "inconclusive: no standard
benchmark" by construction. This curates a SMALL, license-clean pack once, on the host:

    set -a; . ./.env; set +a
    python -m ops.build_benchmark_pack [--dir /mnt/data/labfoundry-benchmarks] [--force]

Datasets (all tiny, all permissive/attribution licenses, normalized to flat JSONL):
  * gsm8k_test.jsonl    — 1.3k grade-school math word problems (MIT, openai/grade-school-math)
  * truthfulqa.jsonl    — 817 truthfulness/calibration questions (Apache-2.0, sylinrl/TruthfulQA)
  * boolq_dev.jsonl     — 3.3k yes/no reading questions (CC BY-SA 3.0, google-research-datasets)
  * humaneval.jsonl     — 164 code-generation problems (MIT, openai/human-eval)

Writes manifest.json (name, file, task, n, fields, license, source, sha256) — the design
prompt renders it so the designer knows exactly what /data holds — and LICENSES.md with
attribution. Idempotent: existing files are kept unless --force.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sys

import httpx

DEFAULT_DIR = os.environ.get("EXPERIMENT_DATASETS_DIR", "/mnt/data/labfoundry-benchmarks")

GSM8K_URL = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
TRUTHFULQA_URL = "https://raw.githubusercontent.com/sylinrl/TruthfulQA/main/TruthfulQA.csv"
BOOLQ_URL = "https://datasets-server.huggingface.co/rows?dataset=google%2Fboolq&config=default&split=validation"
HUMANEVAL_URL = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"


def _fetch(url: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=120)
    r.raise_for_status()
    return r.content


def _gsm8k(raw: bytes) -> list[dict]:
    rows = []
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        answer = d["answer"]
        final = answer.rsplit("####", 1)[-1].strip().replace(",", "") if "####" in answer else None
        rows.append({"question": d["question"], "answer": answer, "final_answer": final})
    return rows


def _truthfulqa(raw: bytes) -> list[dict]:
    rows = []
    for d in csv.DictReader(io.StringIO(raw.decode())):
        rows.append(
            {
                "question": d["Question"],
                "best_answer": d["Best Answer"],
                "correct_answers": [a.strip() for a in d["Correct Answers"].split(";") if a.strip()],
                "incorrect_answers": [a.strip() for a in d["Incorrect Answers"].split(";") if a.strip()],
                "category": d["Category"],
            }
        )
    return rows


def _boolq(_raw: bytes) -> list[dict]:
    # The old storage.googleapis.com/boolq JSONL is gone; page the HF datasets-server
    # rows API instead (100 rows/request). `_raw` (the first page) is refetched here so
    # the pagination stays self-contained.
    rows: list[dict] = []
    offset = 0
    while True:
        page = json.loads(_fetch(f"{BOOLQ_URL}&offset={offset}&length=100"))
        batch = page.get("rows", [])
        if not batch:
            break
        for r in batch:
            d = r["row"]
            rows.append({"question": d["question"], "passage": d["passage"], "answer": bool(d["answer"])})
        offset += len(batch)
        if offset >= page.get("num_rows_total", 0):
            break
    return rows


def _humaneval(raw: bytes) -> list[dict]:
    rows = []
    for line in gzip.decompress(raw).decode().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        rows.append(
            {
                "task_id": d["task_id"],
                "prompt": d["prompt"],
                "canonical_solution": d["canonical_solution"],
                "test": d["test"],
                "entry_point": d["entry_point"],
            }
        )
    return rows


DATASETS = [
    {
        "name": "gsm8k_test",
        "url": GSM8K_URL,
        "normalize": _gsm8k,
        "task": "math word problems (free-form numeric answer)",
        "fields": "question, answer (worked solution), final_answer (numeric string)",
        "license": "MIT",
        "source": "github.com/openai/grade-school-math",
    },
    {
        "name": "truthfulqa",
        "url": TRUTHFULQA_URL,
        "normalize": _truthfulqa,
        "task": "truthfulness / calibration QA",
        "fields": "question, best_answer, correct_answers[], incorrect_answers[], category",
        "license": "Apache-2.0",
        "source": "github.com/sylinrl/TruthfulQA",
    },
    {
        "name": "boolq_dev",
        "url": BOOLQ_URL,
        "normalize": _boolq,
        "task": "yes/no reading comprehension",
        "fields": "question, passage, answer (bool)",
        "license": "CC BY-SA 3.0",
        "source": "github.com/google-research-datasets/boolean-questions",
    },
    {
        "name": "humaneval",
        "url": HUMANEVAL_URL,
        "normalize": _humaneval,
        "task": "Python code generation (unit-test graded)",
        "fields": "task_id, prompt, canonical_solution, test, entry_point",
        "license": "MIT",
        "source": "github.com/openai/human-eval",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(prog="ops.build_benchmark_pack")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--force", action="store_true", help="re-download even if a file exists")
    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)

    manifest = []
    licenses = ["# Benchmark pack — licenses & attribution\n"]
    for ds in DATASETS:
        path = os.path.join(args.dir, f"{ds['name']}.jsonl")
        if os.path.exists(path) and not args.force:
            print(f"  = {ds['name']}: exists, kept")
            with open(path) as f:
                rows = [json.loads(line) for line in f]
        else:
            print(f"  ↓ {ds['name']}: {ds['url']}")
            try:
                rows = ds["normalize"](_fetch(ds["url"]))
            except Exception as e:  # noqa: BLE001 — one dead source must not sink the pack
                print(f"  ✗ {ds['name']}: FAILED ({e}) — skipped; re-run later or fix the source")
                continue
            with open(path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            print(f"  ✓ {ds['name']}: {len(rows)} rows")
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        manifest.append(
            {
                "name": ds["name"],
                "file": f"{ds['name']}.jsonl",
                "task": ds["task"],
                "n": len(rows),
                "fields": ds["fields"],
                "license": ds["license"],
                "source": ds["source"],
                "sha256": sha,
            }
        )
        licenses.append(f"- **{ds['name']}** — {ds['license']}, from {ds['source']}")

    with open(os.path.join(args.dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(args.dir, "LICENSES.md"), "w") as f:
        f.write("\n".join(licenses) + "\n")
    print(f"\npack ready at {args.dir} ({len(manifest)} datasets) — manifest.json + LICENSES.md written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
