"""Mimir's data collectors — discovery sweeps that feed `source.discovered`.

The scouts (library.ingest.scouts) are pure source-finders. This layer runs them
over standing topics, dedupes against the corpus, and emits one
`source.discovered` per NEW source — which Mimir then stages, trust-gates, and
ingests. Keeping emission + dedupe here (not in the scouts) is the §3 design:
scouts stay side-effect-free and trivially testable.

Topics come from LIBRARY_TOPICS (comma-separated env) or a small default set.
Today the only built scout is scout_arxiv; scout_web/scout_github slot in here
the same way (run them and merge the descriptor lists).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime

from library.ingest.scouts import scout_arxiv, scout_dataset, scout_github, scout_openml, scout_web

log = logging.getLogger(__name__)

# Which scouts the sweep runs (LIBRARY_SCOUTS env, comma-separated; default arxiv
# only — web/github/dataset hit external APIs and may need keys/infra). Tests
# patch this dict (monkeypatch.setitem) to inject a fake scout.
_SCOUTS = {
    "arxiv": scout_arxiv,
    "web": scout_web,
    "github": scout_github,
    "dataset": scout_dataset,
    "openml": scout_openml,
}


def _enabled_scout_names() -> list[str]:
    names = [n.strip() for n in os.environ.get("LIBRARY_SCOUTS", "arxiv").split(",") if n.strip()]
    chosen = [n for n in names if n in _SCOUTS]
    return chosen or ["arxiv"]


# Per-scout cap on results/topic. Papers (arXiv) are the base — sweep them as
# deep as the plan asks. Web/GitHub/dataset are bounded supplements: each web
# hit costs a fetch, and 25/topic of marketing blogs is noise, so cap them low
# even when the arXiv depth is high.
_PER_TOPIC_CAP = {"web": 6, "github": 6, "dataset": 8, "openml": 6}


# Broad AI/ML FRONTIER — the standing taxonomy swept when no agenda is set
# (Ariadne/PI inactive). Deliberately wide: in that phase the lab's job is to
# build a strong, diverse base across the field before research begins. Override
# wholesale with LIBRARY_TOPICS (comma-separated).
_AIML_FRONTIER = (
    "large language models",
    "transformer architectures",
    "retrieval augmented generation",
    "in-context learning",
    "instruction tuning",
    "parameter efficient fine-tuning",
    "model quantization",
    "mixture of experts",
    "long context language models",
    "reasoning in language models",
    "chain of thought prompting",
    "llm agents",
    "tool use in language models",
    "multi-agent systems",
    "reinforcement learning from human feedback",
    "ai alignment",
    "language model safety",
    "mechanistic interpretability",
    "hallucination in language models",
    "language model evaluation benchmarks",
    "deep reinforcement learning",
    "offline reinforcement learning",
    "diffusion models",
    "text to image generation",
    "vision transformers",
    "vision language models",
    "multimodal learning",
    "video understanding",
    "speech recognition",
    "text to speech",
    "graph neural networks",
    "knowledge graphs",
    "self-supervised learning",
    "contrastive learning",
    "transfer learning",
    "meta learning",
    "federated learning",
    "continual learning",
    "neural architecture search",
    "knowledge distillation",
    "state space models",
    "time series forecasting",
    "recommender systems",
    "generative adversarial networks",
    "scaling laws for neural networks",
    "efficient transformers",
    "ai for science",
    "robot learning",
)
_DEFAULT_TOPICS = _AIML_FRONTIER  # discovery_topics() default when LIBRARY_TOPICS unset

# Sweep sizing (all env-overridable). Aggressive (no-agenda) base-building runs
# CONTINUOUSLY via the harness's backpressure pump, so each sweep is a small,
# fast slice (not a big periodic burst) — the pump just keeps firing the next
# slice whenever intake runs low. Agenda mode tracks active claims with a light
# frontier top.
_AGGRESSIVE_TOPICS = int(os.environ.get("LIBRARY_AGGRESSIVE_TOPICS", "6"))
_AGGRESSIVE_PER_TOPIC = int(os.environ.get("LIBRARY_AGGRESSIVE_PER_TOPIC", "20"))
_AGENDA_FRONTIER = int(os.environ.get("LIBRARY_AGENDA_FRONTIER", "4"))
_AGENDA_PER_TOPIC = int(os.environ.get("LIBRARY_PER_TOPIC", "8"))
_MAX_AGENDA_TOPICS = 10
# Rotation advances a slice every ~90s so back-to-back pump sweeps cover fresh
# subfields (instead of re-requesting the same slice and finding nothing new).
_ROTATION_PERIOD_S = int(os.environ.get("LIBRARY_ROTATION_PERIOD_S", "90"))

# Stateful discovery (migration 003). A per-source pagination cursor walks the
# back-catalogue: re-grab the newest every _REFRESH_AFTER_S, otherwise page
# deeper, wrapping past _MAX_OFFSET. The seen-ledger retries a source that failed
# to ingest only after _RETRY_AFTER_S (not every sweep, not never).
_REFRESH_AFTER_S = float(os.environ.get("LIBRARY_REFRESH_AFTER_S", "7200"))  # 2h
_MAX_OFFSET = int(os.environ.get("LIBRARY_MAX_OFFSET", "1000"))
_RETRY_AFTER_S = float(os.environ.get("LIBRARY_RETRY_AFTER_S", "43200"))  # 12h


def discovery_topics() -> list[str]:
    """The standing FRONTIER topics (LIBRARY_TOPICS env, else the broad AI/ML
    taxonomy) — wide field coverage for base-building and for catching movement
    beyond the active agenda."""
    raw = os.environ.get("LIBRARY_TOPICS", "")
    topics = [t.strip() for t in raw.split(",") if t.strip()]
    return topics or list(_DEFAULT_TOPICS)


def _rotation_index() -> int:
    """A monotonically advancing index (one step per _ROTATION_PERIOD_S) used to
    rotate which slice of the frontier a sweep covers, so coverage walks the
    whole taxonomy over time rather than re-hitting the same head."""
    return int(datetime.now(UTC).timestamp() // _ROTATION_PERIOD_S)


def _rotate(seq: list[str], rot: int, k: int) -> list[str]:
    """A length-k window into `seq` starting at offset (rot*k), wrapping around."""
    n = len(seq)
    if n == 0:
        return []
    k = min(k, n)
    start = (rot * k) % n
    return [seq[(start + i) % n] for i in range(k)]


async def _active_claim_topics(state) -> list[str]:
    """The active claims' statements — what the lab is working on now. Empty when
    no agenda is set."""
    try:
        claims = await state.get_active_claims(limit=6)
        return [c.statement.strip() for c in claims if (c.statement or "").strip()]
    except Exception:  # noqa: BLE001 — claim steering is best-effort, never blocks the sweep
        log.exception("collectors: get_active_claims failed; sweeping frontier only")
        return []


def ariadne_active() -> bool:
    """True when the research workflow — Ariadne, the PI — is running, i.e. NOT
    in KNOWLEDGE_CORE_ONLY mode. This is the gate for sweep behaviour: when
    Ariadne is dark the sweep runs in aggressive base-building mode (broad AI/ML
    frontier); when she's steering it tracks her agenda (the active claims).
    Leftover claims from an earlier run do NOT count as Ariadne being active."""
    core_only = os.environ.get("KNOWLEDGE_CORE_ONLY", "").lower() in {"1", "true", "on", "yes"}
    return not core_only


async def plan_sweep(state) -> tuple[list[str], int]:
    """Decide WHAT to sweep and HOW HARD, by whether Ariadne (the PI) is active:

    - ARIADNE ACTIVE (research workflow running): track her agenda — the active
      claim statements plus a light rotating frontier slice — at a gentle
      per-topic depth. This is how the PI steers discovery implicitly.
    - ARIADNE DARK (KNOWLEDGE_CORE_ONLY): aggressive base-building — a wide
      rotating slice of the AI/ML frontier at high per-topic depth, so the lab
      fills the Library broadly across the field before research begins.

    Returns (topics, per_topic).
    """
    frontier = discovery_topics()
    rot = _rotation_index()

    if ariadne_active():
        claim_topics = await _active_claim_topics(state)
        merged: list[str] = []
        seen: set[str] = set()
        for t in [*claim_topics, *_rotate(frontier, rot, _AGENDA_FRONTIER)]:
            key = t.lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(t)
        return merged[:_MAX_AGENDA_TOPICS], _AGENDA_PER_TOPIC

    return _rotate(frontier, rot, _AGGRESSIVE_TOPICS), _AGGRESSIVE_PER_TOPIC


async def default_sweep_topics(state) -> list[str]:
    """The topics for the next sweep (see `plan_sweep`); thin wrapper returning
    just the topic list."""
    topics, _ = await plan_sweep(state)
    return topics


def _source_target_id(canonical_key: str) -> int:
    """A stable positive bigint derived from a source's canonical_key.

    target_id is part of the events unique key, so giving a not-yet-ingested
    source a deterministic id lets a re-emitted `source.discovered` dedupe at the
    event level (corpus-level dedupe via document_exists is the primary guard)."""
    return int.from_bytes(hashlib.blake2b(canonical_key.encode(), digest_size=7).digest(), "big")


async def run_discovery_sweep(
    topics: list[str] | None,
    state,
    *,
    per_topic: int | None = None,
    sort: str = "submittedDate",
    claim_id: int | None = None,
) -> dict:
    """Run the scouts over `topics` and emit `source.discovered` for genuinely-new
    sources. Each scout pages deeper via a per-source cursor (so it doesn't keep
    re-fetching the same newest-N), and a novelty gate — corpus check + seen-ledger
    — surfaces only new or retry-due sources (migration 003).

    With no `topics`, plans the sweep from the agenda (see `plan_sweep`): that
    also picks `per_topic` (aggressive when Ariadne is dark, gentle when she's
    steering) unless an explicit `per_topic` is given.

    ALWAYS settles: emits one `library.sweep_settled` carrying {claim_id, scanned,
    discovered, errors} even on zero yield — "the scouts ran and found nothing" must
    be distinguishable from "the scouts never ran" (scanned == 0 ⇒ the sweep was
    BLIND: outage / API cooldown / swallowed non-200s), or the closure ladder
    launders an infrastructure failure into a declared research gap.

    Returns {"scanned": <descriptors found>, "discovered": <new emitted>,
    "errors": <scouts that raised>, "topics": [...]}.
    """
    if topics is None:
        topics, planned_per_topic = await plan_sweep(state)
        if per_topic is None:
            per_topic = planned_per_topic
    if per_topic is None:
        per_topic = _AGENDA_PER_TOPIC

    # FETCH — each scout pages deeper via its own cursor (so it never re-fetches
    # the same newest-N). The cursor is per-source; the scout keeps its internal
    # per-topic pacing by taking the whole topic list at one offset.
    descriptors = []
    scout_errors = 0
    for name in _enabled_scout_names():
        scout = _SCOUTS[name]  # looked up live so tests can monkeypatch _SCOUTS
        scout_per_topic = min(per_topic, _PER_TOPIC_CAP.get(name, per_topic))
        try:
            offset = await state.discovery_offset(
                name, "*", page_size=scout_per_topic, refresh_after_s=_REFRESH_AFTER_S, max_offset=_MAX_OFFSET
            )
        except Exception:  # noqa: BLE001 — a cursor failure must not stop discovery
            log.exception("collectors: discovery cursor for %s failed", name)
            offset = 0
        try:
            # `sort` only applies to arXiv (relevance vs newest) — a targeted closure scout
            # passes sort="relevance" so a niche direction's sweep finds on-topic papers
            # rather than the newest arXiv-wide. Other scouts don't take it.
            kw = {"per_topic": scout_per_topic, "start": offset}
            if name == "arxiv":
                kw["sort"] = sort
            descriptors.extend(await scout(topics, **kw))
        except Exception:  # noqa: BLE001 — one scout failing must not sink the sweep
            scout_errors += 1
            log.exception("collectors: scout %s failed", name)

    # NOVELTY GATE — drop anything already in the corpus, then ask the seen-ledger
    # which of the rest are new or retry-due (recording the attempt). Only those
    # are surfaced, so we stop re-emitting sources that keep failing to ingest.
    fresh = [d for d in descriptors if not await state.document_exists(d.source_kind, d.canonical_key)]
    by_kind: dict[str, list[str]] = {}
    for d in fresh:
        by_kind.setdefault(d.source_kind, []).append(d.canonical_key)
    surfaceable: dict[str, set[str]] = {}
    for sk, keys in by_kind.items():
        try:
            surfaceable[sk] = await state.discovery_filter_new(sk, keys, retry_after_s=_RETRY_AFTER_S)
        except Exception:  # noqa: BLE001 — if the ledger fails, don't block discovery
            log.exception("collectors: novelty ledger for %s failed", sk)
            surfaceable[sk] = set(keys)

    new: list[dict] = []
    emitted: set[tuple[str, str]] = set()
    for d in fresh:
        key = (d.source_kind, d.canonical_key)
        if key in emitted or d.canonical_key not in surfaceable.get(d.source_kind, set()):
            continue
        emitted.add(key)
        await state.emit_corpus_event(
            "source.discovered",
            target_type="source",
            target_id=_source_target_id(d.canonical_key),
            payload={"source": d.model_dump()},
            dedup_key=f"discovered-{d.source_kind}-{d.canonical_key}",
        )
        new.append({"title": d.title, "arxiv_id": d.arxiv_id, "why": d.why})

    if new:
        # A digest of what just surfaced — the PI's "pulse" of the field. Emitted
        # per sweep (no dedup); a future PI step can consult library.trends.
        await state.emit_corpus_event(
            "library.trends",
            target_type="trends",
            target_id=0,
            payload={"topics": topics, "count": len(new), "new": new[:20]},
        )

    # SETTLE — unconditional result artifact (the sweep's "I ran, here's what I saw").
    # The closure ladder reads this instead of the request's emission time: no settle =
    # the handler died; scanned == 0 = the sweep was blind. Neither may become a "gap".
    await state.emit_corpus_event(
        "library.sweep_settled",
        target_type="sweep",
        target_id=claim_id or 0,
        payload={
            "claim_id": claim_id,
            "topics": topics,
            "scanned": len(descriptors),
            "discovered": len(new),
            "errors": scout_errors,
        },
    )

    log.info(
        "discovery sweep: %d/%d new sources, %d scout error(s) (topics=%s)",
        len(new),
        len(descriptors),
        scout_errors,
        topics,
    )
    return {"scanned": len(descriptors), "discovered": len(new), "errors": scout_errors, "topics": topics}
