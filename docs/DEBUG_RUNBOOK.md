# Lab Debug & Safe-Change Runbook

How to debug the running lab — and fix things — **without breaking the work the lab has
already done**. Read this before flipping Ariadne to advisory.

## 0. The one invariant that makes this safe

**State is tiered by cost-to-rebuild, and changes touch only the cheap tier.**

| tier | examples | size | rebuild | rule |
|---|---|---|---|---|
| **Expensive but re-derivable** | `chunks`/embeddings | ~19 GB | `make seed-corpus` ~2h (rag-bench data intact) | never `TRUNCATE`/`DROP`; read-only in normal ops |
| | context graph (Neo4j) | — | `ops.extract_concepts_backfill` ~hours | re-derivable from the corpus |
| **Precious + small** | `documents` registry, `certifications` (Mimir decisions), `claims`, `claim_goals` (Ariadne) | ~45 MB | hard to rebuild | **snapshot before any change** |
| **Cheap/regenerable** | `events`, cooldowns | — | regenerate | fine to clear |

**Ariadne writes ONLY the precious-small tier** (`claim_kind='mission'/'direction'` claims + `claim_goals`), never the 19 GB. So rolling back her work never touches the corpus or graph.

## 1. Before you touch anything

```bash
set -a; . ./.env; set +a
python -m ops.lab_snapshot          # ~12MB, seconds — decisions + reasoning + registry
python -m ops.lab_doctor            # baseline health, so you can tell what your change changed
```

## 2. The debug loop  (OBSERVE → DIAGNOSE → ISOLATE → REPRODUCE → FIX → VERIFY → RESUME)

1. **OBSERVE** — `python -m ops.lab_doctor` ("is something wrong, where?"). Live: web `/events` (stream + suppression reasons), `/trace` (session step-DAG), `/` (flow). Use **localhost:8088**, not 127.0.0.1.
2. **DIAGNOSE** — narrow to an agent/event. `/trace` the suspect session; Langfuse for the exact LLM call (`agent_runs.langfuse_trace_id`). Causality by hand: `events.emitted_by_run_id` ↔ `agent_runs.triggered_by_event_id` ↔ `events.consumed_run_id`.
3. **ISOLATE / PAUSE** — `python -m ops.agent_mode set <agent> off`. This pauses ONE agent in ~5s **without stopping the lab or touching data** — everything else keeps running. (Reversible. This is the safe alternative to killing the harness.)
4. **REPRODUCE (no writes)** — run the agent in isolation:
   - substrate agents → their eval harness: `eval.retrieval.evaluate`, `eval.mimir.evaluate` / `eval.mimir.probe_eval`, `eval.scouts.evaluate`, `eval.graph.extract_slice`.
   - Mimir end-to-end → `ops.mimir_firstlight` (idempotent driver).
   - Ariadne → `ops.ariadne_firstlight` (**read-only, graded** — the safest debug surface).
   - any registered agent → web `/agents` (Agent Lab, DRY mode: real input + prompt, no writes).
5. **FIX** — edit code; `python -m py_compile <files>`; re-run the agent's eval harness / firstlight to confirm. **Never** run db-fixture `pytest` against the live DB (see §4).
6. **VERIFY** — re-run the eval/firstlight (green), then `ops.lab_doctor` (healthy).
7. **RESUME** — code change → `systemctl --user restart labfoundry-harness` (event-driven + resumable: it picks up where it left off). Mode change → `ops.agent_mode set <agent> active`.

## 3. Per-agent quick reference

| agent | pause | isolate / reproduce | a bug usually means |
|---|---|---|---|
| **mimir** | `agent_mode set mimir off` | `eval.mimir.evaluate` (gold set, pure), `eval.mimir.probe_eval` (live probes), `ops.mimir_firstlight` | a new source type → add a gold case; a probe regressed |
| **scouts** | (via mimir/`LIBRARY_SCOUTS`) | `eval.scouts.evaluate` | a source API changed (e.g. OpenML 412) |
| retrieval | — | `eval.retrieval.evaluate` (recall@k) | index/embedding/ranking regression |
| context graph | — | `eval.graph.extract_slice` | extraction prompt / canonicalization |
| **ariadne** | `agent_mode set ariadne off` | `ops.ariadne_firstlight` (read-only + graded) | grounding/citation fail → her grade gate blocks persistence anyway |

## 4. Safe-change rules (the "don't break the work" part)

- **CODE** changes never touch data. Edit → `py_compile` → eval/pure-test → restart harness (resumable).
- **SCHEMA** changes: idempotent + additive only (`ADD COLUMN ... IF NOT EXISTS` w/ default; `CREATE ... IF NOT EXISTS`). Apply the single new migration **directly via psql** — NOT `make migrate` (it re-runs the non-idempotent baseline `001`). Re-apply once to confirm it's clean.
- **DATA** changes: `ops.lab_snapshot` first; change inside a transaction; verify; keep the snapshot.
- **NEVER**: `pytest` with the `db` fixture against live (its `TRUNCATE … CASCADE` reaches `documents`→`chunks` — this wiped the corpus once; `conftest.py` now refuses if `documents>100`, but don't lean on the guard). No `TRUNCATE`/`DROP`/`CASCADE` on live. Run pure tests only, or point `DATABASE_URL` at a throwaway DB.

## 5. Pre-flight for starting Ariadne

1. `python -m ops.lab_snapshot` (capture pre-Ariadne state).
2. **Shadow first**: `python -m ops.ariadne_firstlight` — read-only, confirm she passes grading (citations resolve). Nothing written.
3. **Advisory**: `python -m ops.agent_mode set ariadne advisory`, then trigger a `ariadne.deliberate` event. She persists `proposed` mission/direction claims + claim_goals — **only if grading passes** (no hallucinated-citation agendas reach the lab).
4. **Keep downstream off**: Planner/research agents default `off` (mode dial), so her `proposed` directions don't spawn tasks until you review and approve.
5. **Review**: inspect her directions (`/claims`, or `SELECT … FROM claims WHERE claim_kind='direction' AND status='proposed'`).
6. **Rollback (surgical, safe)** if her output is bad:
   ```sql
   DELETE FROM claim_goals WHERE claim_id IN (SELECT id FROM claims WHERE claim_kind IN ('mission','direction'));
   DELETE FROM claims WHERE claim_kind IN ('mission','direction');
   ```
   This removes ONLY her work — the corpus, graph, and certifications are untouched. (Or restore the §1 snapshot.)
7. **Pause anytime**: `python -m ops.agent_mode set ariadne off`.

## TL;DR

Snapshot (cheap) → pause the agent (mode dial, reversible) → reproduce read-only (eval / firstlight / Agent-Lab DRY) → fix code → verify (eval + doctor) → resume (restart / mode active). The 19 GB corpus and the graph are read-only to agents and re-derivable; Ariadne's work is isolated and surgically reversible. You cannot lose the lab's accumulated work by debugging it this way.
