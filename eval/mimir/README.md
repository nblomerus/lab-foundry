# Mimir trust-gate evaluation

A frozen, offline gold set for `library.trust.classify_trust` — the deterministic
input gate that decides whether a source may enter the trusted Library, and at what
tier. This is the substrate pillar behind the readiness rule **"no unverified source
enters the trusted Library."**

## Run it

```bash
PY=/home/nicholas/.pyenv/versions/labfoundry/bin/python
$PY -m eval.mimir.evaluate           # full report + safety metrics
$PY -m eval.mimir.evaluate --strict  # exit 1 on ANY mismatch (CI)
pytest tests/test_trust_goldset.py   # regression guard (pure, no live DB)
```

`classify_trust` is a **pure** function over a pre-resolved `DocMeta` (no DB, no
network, no clock), so the gold set is fully deterministic and runs with nothing up.

## What it proves (and what it doesn't)

Cases are labelled by **intended policy**, not by reading the code back — so a
mismatch is a real finding. 57 cases cover every gate branch plus an adversarial
`spoof` set:

- every tier: `peer_reviewed` (resolving DOI), `preprint` (arXiv), `official_repo`
  (active GitHub), `web_reputable` (curated hosts + .edu/.gov), `web_unknown`;
- both hard-gates: retraction and blocked-license, asserted to override **every**
  tier (even a resolving-DOI paper with a `noindex` license is quarantined);
- the boundaries: GitHub release/push 364 vs 365 days; non-resolving DOI falling
  through; GitHub-without-release landing `web_unknown` *without* the LLM tie-breaker;
- the **spoofs** (the point): `arxiv.org.evil.com`, `evilarxiv.org`,
  `github.com.attacker.net`, `wikipedia.org.phishing.com`, `*.mit.edu.evil.com`,
  `myedu.com`, and the userinfo trick `https://arxiv.org@evil.com` — each asserted
  to be **incapable** of minting a trusted tier.

**Baseline (2026-06-06): 57/57, FALSE-ADMIT 0, SPOOF-LEAK 0, OVER-BLOCK 0.** The
deterministic gate is provably correct on every case here.

## Network-signal probes (`probe_eval.py`)

`classify_trust` is only as good as the `DocMeta` that `agents.mimir.handler.
_resolve_signals` fills via live probes. `python -m eval.mimir.probe_eval` checks
them against verified ground truth, with a **canary per group** so an API outage is
reported as SKIP, not a false FAIL, and with the arXiv-withdrawal ground truth
discovered **live** (no guessed ids):

- **doi_resolves** — real journal DOI resolves; fabricated DOI does not (can't mint
  peer_reviewed). ✓
- **doi_retracted** — Crossref flags the Surgisphere/Lancet retraction; a clean paper
  is not flagged. ✓ **Finding:** the Wakefield MMR DOI is retracted in reality but
  Crossref does **not** flag it via the checked fields — a real coverage gap.
- **github** — active repo (releases, recent push) vs frozen repo (no release / stale). ✓
- **arxiv_withdrawn** — discover withdrawn papers live via `abs:"this paper has been
  withdrawn"` (a free-text "withdrawn" query returns papers ABOUT withdrawal, not
  actually-withdrawn ones), confirm the probe flags them; control = a non-withdrawn
  paper. SKIPs when arXiv is unreachable. ✓

Baseline 2026-06-06: doi_resolves 2/2, doi_retracted 3/3, github 2/2,
arxiv_withdrawn 4/4 — all green (after easing the arXiv 429 rate-limit; see below).

> **Safety — fail-closed (fixed 2026-06-06).** The probes are now TRI-STATE
> (retracted / clean / **unknown**), and `ingest_source` **fails closed** by default
> (`MIMIR_RETRACTION_STRICT`, on): when a retraction check is applicable but the probe
> can't verify (arXiv/Crossref outage), the source is **held** (quarantined with a
> `retraction_unverified` signal) instead of admitted as clean. Set
> `MIMIR_RETRACTION_STRICT=off` to admit-and-flag instead (trades safety for
> availability during outages). **Residual gap:** a Crossref *coverage* miss (e.g. the
> Wakefield DOI) returns CLEAN (not unknown), so it still slips through — closing that
> needs a **second retraction source** (e.g. Retraction Watch), a separate enhancement.

### Still unmeasured

1. **LLM tie-breaker quality** — the `needs_llm` web_reputable/web_unknown decision
   (non-deterministic; needs a router/curator + frozen prompts). The gold set only
   verifies `needs_llm` is correctly *flagged*, not what the LLM then decides.
2. **Dedup + provenance recording** — `stage_source` dedup and the `certifications`
   write are covered by `tests/test_acquire.py` / `tests/test_mimir_certify.py`.
