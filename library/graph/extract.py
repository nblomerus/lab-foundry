"""
Concept extraction → context-graph projection (the "why it matters" layer).

The corpus graph is a flat Paper-[:FROM]->Source provenance projection — Ariadne's
Field Model needs the *reasoning* structure: which methods a paper uses, which
datasets it evaluates on, which tasks it addresses, so she can traverse
paper → method → paper and reason about novelty gaps.

rag-bench's entity_extractor does this per-CHUNK (LLM call per chunk → ~1.7M calls
for our corpus — impractical). We extract per-PAPER instead: ONE schema-guided LLM
call over title + abstract + a lead excerpt. That captures a paper's headline
methods/datasets/tasks at ~1/45th the cost (~31k calls for the whole corpus), which
is what the Field Model actually needs. Schema (labels + relations) mirrors
rag-bench's graph_store so a per-chunk pass could enrich the same graph later.

Projection (name-keyed concept nodes, distinct from the id-keyed :Dataset registry):
  (:Paper {id})-[:USES]->(:METHOD {key,name})
  (:Paper {id})-[:EVALUATED_ON]->(:DATASET {key,name})
  (:Paper {id})-[:ADDRESSES]->(:TASK {key,name})
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

from library.graph.tools import _get_driver

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
GRAPH_EXTRACT_MODEL = os.environ.get("GRAPH_EXTRACT_MODEL", "qwen2.5:14b-instruct-q4_K_M")

_PROMPT = """You extract structured metadata from an AI/ML research paper.
Return ONLY a JSON object with keys "methods", "datasets", "tasks" — each a list of
short canonical names (strings, 0-8 each, [] if none):
- methods: techniques/algorithms/architectures the paper uses or proposes
  (e.g. "attention", "LoRA", "contrastive learning", "diffusion").
- datasets: named benchmarks/datasets used for training or evaluation (e.g. "ImageNet", "GLUE", "MMLU").
- tasks: ML tasks addressed (e.g. "machine translation", "image classification").
Use the paper's own terminology. Do not invent items not supported by the text.

TITLE: {title}

ABSTRACT/EXCERPT:
{body}
"""

_CONCEPT_KEYS = ("methods", "datasets", "tasks")


# Acronym ↔ expansion aliases (normalized phrase -> canonical key). Mechanical
# normalization below handles case/hyphen/space/plural; this handles what it can't
# (acronyms vs spelled-out forms). Keep keys short + lowercase.
_CONCEPT_ALIASES = {
    "llm": "llm",
    "llms": "llm",
    "large language model": "llm",
    "large language models": "llm",
    "vlm": "vlm",
    "vision language model": "vlm",
    "vision language models": "vlm",
    "mllm": "mllm",
    "multimodal large language model": "mllm",
    "multimodal large language models": "mllm",
    "rag": "rag",
    "retrieval augmented generation": "rag",
    "rl": "rl",
    "reinforcement learning": "rl",
    "rlhf": "rlhf",
    "reinforcement learning from human feedback": "rlhf",
    "sft": "sft",
    "supervised fine tuning": "sft",
    "cnn": "cnn",
    "convolutional neural network": "cnn",
    "convolutional neural networks": "cnn",
    "convnet": "cnn",
    "rnn": "rnn",
    "recurrent neural network": "rnn",
    "recurrent neural networks": "rnn",
    "gnn": "gnn",
    "graph neural network": "gnn",
    "graph neural networks": "gnn",
    "gan": "gan",
    "generative adversarial network": "gan",
    "generative adversarial networks": "gan",
    "vae": "vae",
    "variational autoencoder": "vae",
    "variational autoencoders": "vae",
    "vit": "vit",
    "vision transformer": "vit",
    "vision transformers": "vit",
    "moe": "moe",
    "mixture of experts": "moe",
    "ppo": "ppo",
    "proximal policy optimization": "ppo",
    "dpo": "dpo",
    "direct preference optimization": "dpo",
    "nlp": "nlp",
    "natural language processing": "nlp",
    "cot": "cot",
    "chain of thought": "cot",
    "svm": "svm",
    "support vector machine": "svm",
    "support vector machines": "svm",
    "knn": "knn",
    "k nearest neighbors": "knn",
    "bert": "bert",
    "gpt": "gpt",
    "t5": "t5",
    "resnet": "resnet",
    "lora": "lora",
    "low rank adaptation": "lora",
    "qlora": "qlora",
    "peft": "peft",
    "parameter efficient fine tuning": "peft",
    "ssm": "ssm",
    "state space model": "ssm",
    "state space models": "ssm",
    "mamba": "mamba",
    # bare 'diffusion' and 'diffusion model(s)' are the SAME concept — without this they split
    # into two nodes with opposite trend verdicts (953p HOT vs 489p SATURATED).
    "diffusion": "diffusion",
    "diffusion model": "diffusion",
    "diffusion models": "diffusion",
}


def _singularize(key: str) -> str:
    """Conservative singular of a despaced concept key. Fixes the over-strip bug of a naive
    'drop trailing s' (which produced processes→processe, analysis→analysi, gaussianprocesses→
    gaussianprocesse — fragmenting concepts that could then never merge):
      • words ending ss/us/is/sis/ics/ies/ous are NOT plurals → left intact (process, status,
        analysis, hypothesis, physics, series, continuous);
      • '-sses' double-s plurals drop 'es' so they merge with the singular (processes→process,
        classes→class, masses→mass, gaussianprocesses→gaussianprocess);
      • any other plain trailing 's' is a regular plural and is dropped (models→model,
        networks→network, databases→database).
    Keys ≤4 chars (acronym plurals: cots/gans/vaes) are left alone."""
    if len(key) <= 4 or not key.endswith("s"):
        return key
    if key.endswith("sses"):
        return key[:-2]
    if key.endswith(("ss", "us", "is", "sis", "ics", "ies", "ous")):
        return key
    return key[:-1]


def _canon_key(name: str) -> str:
    """Canonical dedup key: alias map (acronyms) → else mechanical (lowercase, drop
    hyphens+spaces, conservative singular via _singularize). Merges fine-tuning/finetuning/
    fine tuning, LLM/LLMs, diffusion/diffusion models, RAG/retrieval-augmented generation, etc."""
    n = re.sub(r"\s+", " ", name.strip().lower())
    spaced = re.sub(r"\s+", " ", n.replace("-", " ")).strip()
    if spaced in _CONCEPT_ALIASES:
        return _CONCEPT_ALIASES[spaced]
    key = re.sub(r"[-\s]+", "", n)  # drop hyphens + spaces
    key = _singularize(key)
    return _CONCEPT_ALIASES.get(key, key)


def _norm(name: str) -> dict | None:
    """Normalize a concept name to a (key, display) pair. key = canonical dedup key
    (see _canon_key); drops junk (too short/long, pure punctuation/numbers)."""
    if not isinstance(name, str):
        return None
    disp = re.sub(r"\s+", " ", name).strip().strip(".,;:")
    if not (2 <= len(disp) <= 60) or not re.search(r"[A-Za-z]", disp):
        return None
    return {"key": _canon_key(disp), "name": disp}


def _parse(raw: str) -> dict:
    """Parse the model's JSON (tolerant: strip code fences, find the object)."""
    txt = raw.strip()
    if "```" in txt:
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.MULTILINE).strip()
    try:
        data = json.loads(txt)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            return {k: [] for k in _CONCEPT_KEYS}
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {k: [] for k in _CONCEPT_KEYS}
    out: dict[str, list] = {}
    for k in _CONCEPT_KEYS:
        seen, items = set(), []
        for raw_name in (data.get(k) or [])[:8]:
            c = _norm(raw_name)
            if c and c["key"] not in seen:
                seen.add(c["key"])
                items.append(c)
        out[k] = items
    return out


async def extract_paper_concepts(title: str, body: str, *, model: str = GRAPH_EXTRACT_MODEL) -> dict:
    """ONE schema-guided LLM call → {methods, datasets, tasks} as [{key,name}].
    Deterministic JSON via Ollama's format=json. Empty lists on any failure."""
    prompt = _PROMPT.format(title=(title or "Untitled")[:300], body=(body or "")[:4000])
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                # keep_alive holds the model resident between papers — without it the
                # discovery pump's embedding bursts let the 14B idle out and cold-reload
                # every call (~2-3x slower; ollama ps shows nothing loaded). 30m >> the
                # per-paper gap, so it stays warm for the whole backfill.
                json={
                    "model": model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"temperature": 0.0},
                },
            )
            resp.raise_for_status()
            return _parse(resp.json().get("response", ""))
    except Exception as e:  # noqa: BLE001 — best-effort; one paper failing must not sink a sweep
        log.warning("extract_paper_concepts failed for %r: %s", (title or "")[:60], e)
        return {k: [] for k in _CONCEPT_KEYS}


# Label + edge per concept kind (mirrors rag-bench graph_store conventions).
_PROJECTION = {
    "methods": ("METHOD", "USES"),
    "datasets": ("DATASET", "EVALUATED_ON"),
    "tasks": ("TASK", "ADDRESSES"),
}


async def project_paper_concepts(paper_id: int, concepts: dict) -> dict:
    """MERGE the concept nodes (name-keyed) + typed edges from the Paper. Idempotent.
    Returns counts written. Best-effort: logs and continues on failure."""
    written = {"methods": 0, "datasets": 0, "tasks": 0}
    try:
        driver = await _get_driver()
        async with driver.session() as session:
            # The marker makes the backfill resumable AND distinguishes a
            # genuinely-concept-free paper (math/management) from an un-processed one,
            # so 0-concept papers aren't re-extracted every run.
            await session.run("MERGE (p:Paper {id: $id}) SET p.concepts_extracted = true", id=paper_id)
            for kind, (label, rel) in _PROJECTION.items():
                items = concepts.get(kind) or []
                if not items:
                    continue
                # Fixed label/rel (no injection); only names are parameters.
                await session.run(
                    f"""
                    MATCH (p:Paper {{id: $id}})
                    UNWIND $items AS c
                      MERGE (n:{label} {{key: c.key}}) SET n.name = c.name
                      MERGE (p)-[:{rel}]->(n)
                    """,
                    id=paper_id,
                    items=items,
                )
                written[kind] = len(items)
    except Exception:  # noqa: BLE001
        log.exception("project_paper_concepts failed for paper %s — continuing", paper_id)
    return written


async def ensure_concept_constraints() -> None:
    """Idempotent uniqueness on the name-keyed concept nodes + the resume-marker index."""
    driver = await _get_driver()
    async with driver.session() as session:
        for label in ("METHOD", "DATASET", "TASK"):
            await session.run(
                f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:{label}) REQUIRE n.key IS UNIQUE"
            )
        await session.run("CREATE INDEX paper_concepts_extracted IF NOT EXISTS FOR (p:Paper) ON (p.concepts_extracted)")


async def extracted_paper_ids() -> set[int]:
    """Paper ids already concept-extracted (the resume set for the backfill)."""
    driver = await _get_driver()
    async with driver.session() as session:
        res = await session.run("MATCH (p:Paper) WHERE p.concepts_extracted = true RETURN p.id AS id")
        return {r["id"] async for r in res}
