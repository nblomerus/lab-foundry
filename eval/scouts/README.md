# Scout (discovery-layer) evaluation

The five scouts (arXiv / Web / GitHub / OpenML / HF-dataset) are the Library's front
door — they discover candidates Mimir then trust-gates. Mimir filters *untrustworthy*
sources, but it can't fix *irrelevant, empty, or malformed* discovery; that just
starves the pipeline. This LIVE eval measures, per scout:

- **CONTRACT** — every `SourceDescriptor` well-formed (correct `source_kind`/`kind`,
  non-empty `canonical_key` + `url`). Deterministic; a violation is a bug. (FAIL)
- **DEDUP** — no duplicate `canonical_key` within a run. (FAIL)
- **LIVENESS w/ reachability canary** — distinguishes **SKIP** (source itself is
  down → not the scout's fault) from **EMPTY** (source answers 200 but the scout
  returned 0 → a real scout weakness).
- **RELEVANCE@k** — *coarse lexical* proxy (fraction of titles containing a topic
  token). A signal, not a gate — repos/datasets name things, so a low score can be
  naming, not irrelevance.
- **ROBUSTNESS** — empty topic list returns `[]` with no network call / no crash.

```bash
set -a; . ./.env; set +a    # SEARXNG_URL, GITHUB_TOKEN
PY=/home/nicholas/.pyenv/versions/labfoundry/bin/python
$PY -m eval.scouts.evaluate
```

Cross-source dedup (same paper from arXiv *and* web) is the Librarian handler's job
(dedupe by `(source_kind, canonical_key)`), not a scout's — out of scope here.

## Baseline (2026-06-06)

```
  arxiv     SKIP   source unreachable (transient backoff — re-run when arXiv is up)
  web       PASS   n=5  contract ok  dedup ok
  github    PASS   n=5  contract ok  dedup ok   (relevance@k low = repo-name proxy)
  dataset   PASS   n=5  contract ok  dedup ok
  openml    EMPTY  source UP (200) but scout returned 0 — scout ineffective
```

### Findings

1. **OpenML scout is effectively non-functional for topic discovery.** It queries
   `data/list/data_name/<keyword>`, but OpenML's `data_name` is an **exact match**
   that returns **HTTP 412 "No results"** on a miss. Confirmed: `mnist` → 412 (the
   real dataset is `mnist_784`); `mnist_784`/`iris`/`credit-g` → 200. Real research
   topics almost never equal a dataset name exactly, so the scout returns 0 for
   nearly everything. (Not enabled by default — `LIBRARY_SCOUTS` is `arxiv` — so it
   isn't harming the live lab, but it is dead weight.) Fix: match a topic to datasets
   by **substring/search** over a fetched candidate set, or use OpenML's newer search
   API — not exact `data_name`.
2. **Scouts (except web) swallow non-200 responses into `[]`.** A broken or
   rate-limited source is then indistinguishable downstream from "topic had no
   matches." `scout_web` logs a warning on non-200; the others should too. This eval
   works around it with the reachability canary (→ EMPTY vs SKIP), but the scouts
   themselves hide the signal.
3. **relevance@k is a coarse proxy.** GitHub's ~0.20 reflects that repo *full_names*
   rarely contain the topic word even when relevant — not low quality. Treat the
   number as directional.
