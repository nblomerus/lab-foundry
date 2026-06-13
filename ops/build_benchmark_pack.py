"""
Build the lab's offline BENCHMARK PACK — the /data the experiment sandbox mounts.

The sandbox is --network none: experiments can reach local models (the LLM broker) but
had no real evaluation data, so model-behaviour findings were "inconclusive: no standard
benchmark" by construction. This curates a SMALL, license-clean pack once, on the host:

    set -a; . ./.env; set +a
    python -m ops.build_benchmark_pack [--dir /mnt/data/labfoundry-benchmarks] [--force]

Datasets (all tiny, all permissive/attribution licenses, normalized to flat JSONL). Two families:

  TEXT / LLM benchmarks (for EXPERIMENT_LLM_BROKER model-behaviour runs):
  * gsm8k_test.jsonl    — 1.3k grade-school math word problems (MIT, openai/grade-school-math)
  * truthfulqa.jsonl    — 817 truthfulness/calibration questions (Apache-2.0, sylinrl/TruthfulQA)
  * boolq_dev.jsonl     — 3.3k yes/no reading questions (CC BY-SA 3.0, google-research-datasets)
  * humaneval.jsonl     — 164 code-generation problems (MIT, openai/human-eval)
  * mmlu / arc / hellaswag — multiple-choice reasoning (HF datasets-server, capped)

  TABULAR / classical-ML (for GP/SVM/XGBoost/calibration/uncertainty runs — the lab's bread
  and butter, which previously had NO real data to point at, forcing make_classification):
  * adult            — income classification, mixed cat/num, class imbalance (UCI, CC BY 4.0)
  * wine_quality_red — ordinal regression, noisy labels → calibration (UCI, CC BY 4.0)
  * california_housing — regression (StatLib, public domain)
  * covtype_sample   — multiclass forest-cover, 10k slice (UCI, CC BY 4.0)

Each dataset entry carries either `url`+`normalize` (fetch one blob, parse) or a `loader`
callable (multi-step / paged / subsampled fetch returning rows directly). Large sets are
SUBSAMPLED at build time with a fixed seed so the slice is small and its sha256 is stable.

Writes manifest.json (name, file, modality, task, task_type, n, fields, license, source,
sha256) — the design prompt + the PI's proposal prompt render it so both know exactly what
/data holds — and LICENSES.md with attribution. Idempotent: existing files are kept unless --force.
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
from collections.abc import Callable
from urllib.parse import quote

import httpx
import numpy as np

DEFAULT_DIR = os.environ.get("EXPERIMENT_DATASETS_DIR", "/mnt/data/labfoundry-benchmarks")

GSM8K_URL = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
TRUTHFULQA_URL = "https://raw.githubusercontent.com/sylinrl/TruthfulQA/main/TruthfulQA.csv"
BOOLQ_URL = "https://datasets-server.huggingface.co/rows?dataset=google%2Fboolq&config=default&split=validation"
HUMANEVAL_URL = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"

# Tabular (classical-ML) sources — authoritative, permissively licensed.
ADULT_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/adult/adult.data"
WINE_RED_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv"
CALIFORNIA_URL = "https://raw.githubusercontent.com/ageron/handson-ml2/master/datasets/housing/housing.csv"
COVTYPE_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/covtype/covtype.data.gz"
WDBC_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/breast-cancer-wisconsin/wdbc.data"

SUBSAMPLE_SEED = 0  # fixed → a subsampled slice is deterministic, so its sha256 is stable across builds


def _fetch(url: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=180)
    r.raise_for_status()
    return r.content


def _subsample(rows: list[dict], cap: int) -> list[dict]:
    """Deterministic seeded subsample (stable bytes → stable sha256). Order preserved."""
    if len(rows) <= cap:
        return rows
    rng = np.random.default_rng(SUBSAMPLE_SEED)
    idx = sorted(rng.choice(len(rows), size=cap, replace=False).tolist())
    return [rows[i] for i in idx]


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


# ── tabular (classical-ML) loaders ───────────────────────────────────────────────
def _adult(raw: bytes) -> list[dict]:
    cols = [
        "age",
        "workclass",
        "fnlwgt",
        "education",
        "education_num",
        "marital_status",
        "occupation",
        "relationship",
        "race",
        "sex",
        "capital_gain",
        "capital_loss",
        "hours_per_week",
        "native_country",
    ]
    rows: list[dict] = []
    for line in raw.decode().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 15 or not parts[0]:
            continue
        feats = dict(zip(cols, parts[:14], strict=False))
        for k in ("age", "fnlwgt", "education_num", "capital_gain", "capital_loss", "hours_per_week"):
            feats[k] = int(feats[k])
        feats["label"] = 1 if parts[14].rstrip(".") == ">50K" else 0  # binary income, target imbalance ~24%
        rows.append(feats)
    return _subsample(rows, 5000)


def _wine_quality(raw: bytes) -> list[dict]:
    rows: list[dict] = []
    for d in csv.DictReader(io.StringIO(raw.decode()), delimiter=";"):
        row = {k.replace(" ", "_"): float(v) for k, v in d.items() if k != "quality"}
        row["label"] = int(float(d["quality"]))  # ordinal 0-10, noisy → a real calibration regime
        rows.append(row)
    return rows


def _california(raw: bytes) -> list[dict]:
    rows: list[dict] = []
    for d in csv.DictReader(io.StringIO(raw.decode())):
        row = {}
        ok = True
        for k, v in d.items():
            if k == "median_house_value":
                continue
            if k == "ocean_proximity":
                row[k] = v  # the one categorical column
            else:
                try:
                    row[k] = float(v) if v != "" else None
                except ValueError:
                    ok = False
        if not ok or d.get("median_house_value") in (None, ""):
            continue
        row["label"] = float(d["median_house_value"])  # regression target
        rows.append(row)
    return rows


def _covtype(raw: bytes) -> list[dict]:
    rows: list[dict] = []
    for line in gzip.decompress(raw).decode().splitlines():
        parts = line.split(",")
        if len(parts) != 55:
            continue
        feats = {f"f{i}": int(parts[i]) for i in range(54)}
        feats["label"] = int(parts[54])  # cover type 1-7 (multiclass)
        rows.append(feats)
    return _subsample(rows, 10000)


# ── HF datasets-server pager (no auth) for LLM benchmarks ─────────────────────────
def _hf_loader(
    dataset: str, config: str, split: str, *, cap: int, mapper: Callable[[dict], dict]
) -> Callable[[], list[dict]]:
    """Page the HF datasets-server rows API (100/req) up to `cap`, mapping each row. Returns a
    no-arg loader so the dataset entry can omit `url` (the pager fetches internally, like _boolq)."""
    base = f"https://datasets-server.huggingface.co/rows?dataset={quote(dataset)}&config={quote(config)}&split={split}"

    def _load() -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < cap:
            page = json.loads(_fetch(f"{base}&offset={offset}&length=100"))
            batch = page.get("rows", [])
            if not batch:
                break
            for r in batch:
                out.append(mapper(r["row"]))
            offset += len(batch)
            if offset >= page.get("num_rows_total", 0):
                break
        return out[:cap]

    return _load


def _map_mmlu(d: dict) -> dict:
    return {"question": d["question"], "choices": d["choices"], "answer": d["answer"], "subject": d.get("subject")}


def _map_arc(d: dict) -> dict:
    return {
        "question": d["question"],
        "choices": d["choices"]["text"],
        "labels": d["choices"]["label"],
        "answer": d["answerKey"],
    }


def _map_hellaswag(d: dict) -> dict:
    return {"context": d["ctx"], "endings": d["endings"], "answer": int(d["label"]) if d.get("label", "") != "" else None}


def _map_openbookqa(d: dict) -> dict:
    ch = d.get("choices") or {}
    return {
        "question": d.get("question_stem"),
        "choices": ch.get("text"),
        "labels": ch.get("label"),
        "answer": d.get("answerKey"),
    }


def _map_commonsenseqa(d: dict) -> dict:
    ch = d.get("choices") or {}
    return {
        "question": d.get("question"),
        "choices": ch.get("text"),
        "labels": ch.get("label"),
        "answer": d.get("answerKey"),
    }


def _map_mbpp(d: dict) -> dict:
    return {"task_id": d.get("task_id"), "prompt": d.get("text"), "code": d.get("code"), "test_list": d.get("test_list")}


def _map_winogrande(d: dict) -> dict:
    return {
        "sentence": d.get("sentence"),
        "option1": d.get("option1"),
        "option2": d.get("option2"),
        "answer": d.get("answer"),
    }


def _wdbc(raw: bytes) -> list[dict]:
    """UCI Breast Cancer Wisconsin (Diagnostic): id, diagnosis (M/B), then 30 numeric features."""
    rows = []
    for line in raw.decode().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 32:
            continue
        try:
            feats = [float(x) for x in parts[2:32]]
        except ValueError:
            continue
        rows.append({"features": feats, "label": 1 if parts[1] == "M" else 0})
    return rows


DATASETS = [
    {
        "name": "gsm8k_test",
        "url": GSM8K_URL,
        "normalize": _gsm8k,
        "modality": "text",
        "task_type": "qa",
        "task": "math word problems (free-form numeric answer)",
        "fields": "question, answer (worked solution), final_answer (numeric string)",
        "license": "MIT",
        "source": "github.com/openai/grade-school-math",
    },
    {
        "name": "truthfulqa",
        "url": TRUTHFULQA_URL,
        "normalize": _truthfulqa,
        "modality": "text",
        "task_type": "qa",
        "task": "truthfulness / calibration QA",
        "fields": "question, best_answer, correct_answers[], incorrect_answers[], category",
        "license": "Apache-2.0",
        "source": "github.com/sylinrl/TruthfulQA",
    },
    {
        "name": "boolq_dev",
        "url": BOOLQ_URL,
        "normalize": _boolq,
        "modality": "text",
        "task_type": "classification",
        "task": "yes/no reading comprehension",
        "fields": "question, passage, answer (bool)",
        "license": "CC BY-SA 3.0",
        "source": "github.com/google-research-datasets/boolean-questions",
    },
    {
        "name": "humaneval",
        "url": HUMANEVAL_URL,
        "normalize": _humaneval,
        "modality": "text",
        "task_type": "code",
        "task": "Python code generation (unit-test graded)",
        "fields": "task_id, prompt, canonical_solution, test, entry_point",
        "license": "MIT",
        "source": "github.com/openai/human-eval",
    },
    # ── tabular / classical-ML (real datasets for GP/SVM/XGBoost/calibration/uncertainty) ──
    {
        "name": "adult",
        "url": ADULT_URL,
        "normalize": _adult,
        "modality": "tabular",
        "task_type": "classification",
        "task": "binary income classification (>50K), mixed categorical+numeric, ~24% positive (imbalance)",
        "fields": "14 features (age, workclass, education, occupation, hours_per_week, …) + label (0/1)",
        "license": "CC BY 4.0",
        "source": "archive.ics.uci.edu (UCI Adult / Census Income), 5k seeded slice",
    },
    {
        "name": "wine_quality_red",
        "url": WINE_RED_URL,
        "normalize": _wine_quality,
        "modality": "tabular",
        "task_type": "regression",
        "task": "ordinal wine-quality regression (0-10), noisy human labels — a real calibration regime",
        "fields": "11 physicochemical features (acidity, sulphates, alcohol, …) + label (quality int)",
        "license": "CC BY 4.0",
        "source": "archive.ics.uci.edu (UCI Wine Quality, red)",
    },
    {
        "name": "california_housing",
        "url": CALIFORNIA_URL,
        "normalize": _california,
        "modality": "tabular",
        "task_type": "regression",
        "task": "median house-value regression (20.6k rows) — a standard real-world regression benchmark",
        "fields": "8 numeric features + ocean_proximity (categorical) + label (median_house_value)",
        "license": "public domain (StatLib / Pace & Barry 1997)",
        "source": "raw.githubusercontent.com/ageron/handson-ml2 (StatLib California housing)",
    },
    {
        "name": "covtype_sample",
        "url": COVTYPE_URL,
        "normalize": _covtype,
        "modality": "tabular",
        "task_type": "classification",
        "task": "7-class forest cover-type classification, 10k seeded slice (large-feature, GP/SVM stress)",
        "fields": "54 features (elevation, slope, soil-type one-hots, …) + label (cover type 1-7)",
        "license": "CC BY 4.0",
        "source": "archive.ics.uci.edu (UCI Covertype), 10k seeded slice",
    },
    # ── extra LLM benchmarks (HF datasets-server, capped; for EXPERIMENT_LLM_BROKER runs) ──
    {
        "name": "mmlu",
        "loader": _hf_loader("cais/mmlu", "all", "test", cap=2000, mapper=_map_mmlu),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "massive multitask multiple-choice knowledge (4-way), 2k slice across subjects",
        "fields": "question, choices[4], answer (index), subject",
        "license": "MIT",
        "source": "huggingface.co/datasets/cais/mmlu",
    },
    {
        "name": "arc_challenge",
        "loader": _hf_loader("allenai/ai2_arc", "ARC-Challenge", "test", cap=1172, mapper=_map_arc),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "grade-school science multiple-choice (hard split)",
        "fields": "question, choices[], labels[], answer (label)",
        "license": "CC BY-SA 4.0",
        "source": "huggingface.co/datasets/allenai/ai2_arc",
    },
    {
        "name": "hellaswag",
        "loader": _hf_loader("Rowan/hellaswag", "default", "validation", cap=2000, mapper=_map_hellaswag),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "commonsense sentence-completion (4-way), 2k slice",
        "fields": "context, endings[4], answer (index)",
        "license": "MIT",
        "source": "huggingface.co/datasets/Rowan/hellaswag",
    },
    {
        "name": "openbookqa",
        "loader": _hf_loader("allenai/openbookqa", "main", "test", cap=500, mapper=_map_openbookqa),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "elementary-science multiple-choice with open-book facts (4-way)",
        "fields": "question, choices[4], labels[], answer (label)",
        "license": "Apache-2.0",
        "source": "huggingface.co/datasets/allenai/openbookqa",
    },
    {
        "name": "commonsense_qa",
        "loader": _hf_loader("tau/commonsense_qa", "default", "validation", cap=1221, mapper=_map_commonsenseqa),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "commonsense multiple-choice QA (5-way)",
        "fields": "question, choices[5], labels[], answer (label)",
        "license": "MIT",
        "source": "huggingface.co/datasets/tau/commonsense_qa",
    },
    {
        "name": "mbpp",
        "loader": _hf_loader("google-research-datasets/mbpp", "full", "test", cap=500, mapper=_map_mbpp),
        "modality": "text",
        "task_type": "code",
        "task": "basic Python programming problems (unit-test graded) — complements HumanEval",
        "fields": "task_id, prompt (text), code, test_list[]",
        "license": "CC BY-4.0",
        "source": "huggingface.co/datasets/google-research-datasets/mbpp",
    },
    {
        "name": "winogrande",
        "loader": _hf_loader("allenai/winogrande", "winogrande_xl", "validation", cap=1267, mapper=_map_winogrande),
        "modality": "text",
        "task_type": "multiple_choice",
        "task": "Winograd-schema commonsense coreference (binary option choice)",
        "fields": "sentence, option1, option2, answer (1|2)",
        "license": "CC BY (Apache-2.0 code)",
        "source": "huggingface.co/datasets/allenai/winogrande",
    },
    {
        "name": "breast_cancer_wdbc",
        "url": WDBC_URL,
        "normalize": _wdbc,
        "modality": "tabular",
        "task_type": "classification",
        "task": "binary tumor diagnosis (malignant/benign) from 30 numeric cell-nucleus features",
        "fields": "features[30] (mean/se/worst of radius, texture, …) + label (1=malignant)",
        "license": "CC BY 4.0",
        "source": "archive.ics.uci.edu (UCI Breast Cancer Wisconsin Diagnostic)",
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
            # A dataset carries EITHER a `loader` (no-arg, fetches internally — paged HF /
            # subsampled tabular) OR a `url`+`normalize` pair. Either path returns flat rows.
            print(f"  ↓ {ds['name']}: {ds.get('url') or ds['source']}")
            try:
                rows = ds["loader"]() if ds.get("loader") else ds["normalize"](_fetch(ds["url"]))
            except Exception as e:  # noqa: BLE001 — one dead source must not sink the pack
                print(f"  ✗ {ds['name']}: FAILED ({e}) — skipped; re-run later or fix the source")
                continue
            if not rows:
                print(f"  ✗ {ds['name']}: 0 rows — skipped")
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
                "modality": ds["modality"],
                "task": ds["task"],
                "task_type": ds["task_type"],
                "n": len(rows),
                "fields": ds["fields"],
                "license": ds["license"],
                "source": ds["source"],
                "sha256": sha,
            }
        )
        licenses.append(f"- **{ds['name']}** ({ds['modality']}/{ds['task_type']}) — {ds['license']}, from {ds['source']}")

    with open(os.path.join(args.dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(args.dir, "LICENSES.md"), "w") as f:
        f.write("\n".join(licenses) + "\n")
    print(f"\npack ready at {args.dir} ({len(manifest)} datasets) — manifest.json + LICENSES.md written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
