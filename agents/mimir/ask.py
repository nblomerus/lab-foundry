"""
Mimir's ASK — GraphRAG question answering over the Library.

The point of the hybrid retrieval + context graph was never "fetch me a paper" — it was so an
agent can CONVERSE with the Warden: ask a question that needs MULTI-HOP reasoning over the graph,
and get a synthesized, grounded answer (not a single source). This wires those two together:

  question ──► hybrid corpus_search (the relevant papers)
                     │
                     ├─► concept-graph traversal anchored on those papers:
                     │      paper ─USES/ADDRESSES/EVALUATED_ON─► method/task/dataset (+ corpus coverage)
                     │      method ◄─ papers ─► co-occurring concepts            (2-hop neighbourhood)
                     │
                     └─► DeepSeek synthesises across passages + graph structure → answer + citations + GAPS

GAPS = concepts relevant to the question that the corpus covers THINLY — exactly what an agent should
request next (turns the acquire queue from 'already_have' into real fetches). Read-only.
"""

from __future__ import annotations

import hashlib
import logging
import time

from pydantic import BaseModel, Field

from agents.llm import _chain_complete, _strip_fences
from library.corpus.tools import corpus_search
from library.graph.tools import _get_driver

log = logging.getLogger(__name__)

_REL = "USES|ADDRESSES|EVALUATED_ON"


def _ask_target_id(asker: str, question: str) -> int:
    """Stable bigint for a conversation's events (so ask/answered share a target_id)."""
    return int.from_bytes(hashlib.blake2b(f"{asker}:{question}".encode(), digest_size=7).digest(), "big")


class RetrievedRef(BaseModel):
    document_id: int
    title: str | None = None
    trust_tier: str
    snippet: str


async def retrieve(query: str, *, k: int = 6, exclude_lab: bool = False) -> list[RetrievedRef]:
    """EFFICIENT direct mode — when an agent already knows what it wants, Mimir just gives it:
    the top passages from hybrid retrieval, NO LLM synthesis. (Use answer_question only when the
    request needs multi-hop reasoning.) This is the cheap, fast path Mimir answers most asks with.
    `exclude_lab=True` drops the lab's own first-party artifacts so a researcher gathering EXTERNAL
    evidence can't be handed the lab's own proposal text back as prior art."""
    chunks = await corpus_search(query, k=k, exclude_lab=exclude_lab)
    return [
        RetrievedRef(document_id=c.document_id, title=c.title, trust_tier=c.trust_tier, snippet=c.text[:300].strip())
        for c in chunks
    ]


class MimirAnswer(BaseModel):
    answer: str = Field(..., description="synthesized, multi-hop answer grounded in passages + graph")
    citations: list[str] = Field(default_factory=list, description="paper titles drawn on")
    related_concepts: list[str] = Field(default_factory=list, description="connected concepts from the graph")
    gaps: list[str] = Field(default_factory=list, description="thinly-covered areas relevant to the question")


async def _graph_neighborhood(doc_ids: list[int]) -> str:
    """The concept-graph context around the retrieved papers: their concepts + corpus coverage,
    plus the 2-hop co-occurring concepts (the multi-hop neighbourhood). Best-effort."""
    if not doc_ids:
        return "(no graph context)"
    try:
        driver = await _get_driver()
        async with driver.session() as s:
            direct = []
            res = await s.run(
                f"MATCH (p:Paper)-[r:{_REL}]->(n) WHERE p.id IN $ids "
                "WITH n, type(r) AS rel, count(DISTINCT p) AS local "
                f"MATCH (n)<-[:{_REL}]-(ap:Paper) "
                "RETURN head(labels(n)) AS kind, n.name AS name, rel, local, count(DISTINCT ap) AS total "
                "ORDER BY local DESC, total DESC LIMIT 22",
                ids=doc_ids,
            )
            async for r in res:
                direct.append((r["name"], r["kind"], r["rel"], r["local"], r["total"]))
            co = []
            res2 = await s.run(
                f"MATCH (p:Paper)-[:{_REL}]->(seed) WHERE p.id IN $ids "
                f"MATCH (seed)<-[:{_REL}]-(q:Paper)-[:{_REL}]->(c) WHERE c <> seed "
                "RETURN head(labels(c)) AS kind, c.name AS name, count(DISTINCT q) AS strength "
                "ORDER BY strength DESC LIMIT 14",
                ids=doc_ids,
            )
            async for r in res2:
                co.append((r["name"], r["kind"], r["strength"]))
    except Exception as e:  # noqa: BLE001 — graph is best-effort context
        log.warning("mimir ask: graph neighborhood failed: %s", e)
        return "(concept graph unavailable)"

    lines = ["## Concepts in the retrieved papers — name [kind/relation] · here / total-corpus-coverage"]
    for name, kind, rel, local, total in direct:
        lines.append(f"- {name} [{kind}/{rel}] · {local} here / {total} in corpus")
    if co:
        lines.append("## Co-occurring concepts (2-hop neighbourhood) · strength")
        for name, kind, strength in co:
            lines.append(f"- {name} [{kind}] · {strength}")
    return "\n".join(lines)


_SYSTEM = """You are Mimir, Warden of the Library of an autonomous AI research lab. Answer the QUESTION
by SYNTHESIZING across the retrieved passages AND the concept-graph structure provided — do NOT just
summarize one paper. Use the graph to reason about relationships: which methods address which tasks,
what datasets they're evaluated on, what is well-covered versus thin. Cite the specific papers you draw
on (by title). Then identify GAPS: concepts relevant to the question that the corpus covers THINLY (low
total coverage) — these are where acquiring new evidence would most help."""

_HINT = (
    'Output JSON: {"answer": str, "citations": [paper titles you used], '
    '"related_concepts": [connected concepts from the graph], '
    '"gaps": [specific thinly-covered areas relevant to the question]}'
)


async def _emit(state, event: str, *, asker: str, tid: int, nonce: int, payload: dict) -> None:
    """Best-effort conversation telemetry — lights the Ariadne↔Mimir edge on the floorplan.
    Only emitted when a `state` is supplied (the LIVE deliberation path); the read-only
    firstlight dry-run passes state=None so it stays write-free."""
    if state is None:
        return
    try:
        await state.emit_corpus_event(
            event,
            target_type="conversation",
            target_id=tid,
            payload={"asker": asker, **payload},
            dedup_key=f"{event}-{asker}-{nonce}",
        )
    except Exception as e:  # noqa: BLE001 — telemetry must never break the conversation
        log.warning("mimir ask: emit %s failed: %s", event, e)


async def answer_question(
    question: str, *, k: int = 8, state=None, asker: str = "ariadne", exclude_lab: bool = False
) -> MimirAnswer:
    """Answer a multi-hop question over the Library (retrieval + graph + synthesis). Read-only
    for the corpus. When `state` is given, emits `mimir.ask` (before) and `mimir.answered`
    (after) so the floorplan shows the agent CONVERSING with Mimir in real time.
    `exclude_lab=True` keeps the lab's own proposals/findings out of the synthesised evidence so a
    'gap' is asserted against EXTERNAL literature, not against the lab's own write-up of the gap."""
    tid, nonce = _ask_target_id(asker, question), time.time_ns()
    await _emit(state, "mimir.ask", asker=asker, tid=tid, nonce=nonce, payload={"question": question[:400]})
    chunks = await corpus_search(question, k=k, exclude_lab=exclude_lab)
    doc_ids = list({c.document_id for c in chunks})
    passages = (
        "\n".join(f"[{c.trust_tier}] {(c.title or 'untitled')[:90]} — {c.text[:300].strip()}" for c in chunks)
        or "(no passages retrieved)"
    )
    graph_ctx = await _graph_neighborhood(doc_ids)
    user = (
        f"# Question\n{question}\n\n"
        f"## Retrieved passages (hybrid retrieval over the certified corpus)\n{passages}\n\n"
        f"{graph_ctx}\n\n"
        f"# Task\nAnswer the question, reasoning across the passages and the graph. {_HINT}"
    )
    content = await _chain_complete(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        temperature=0.3,
        invocation_type="mimir.ask",
        step_name="ask",
    )
    answer = MimirAnswer.model_validate_json(_strip_fences(content))
    await _emit(
        state,
        "mimir.answered",
        asker=asker,
        tid=tid,
        nonce=nonce,
        payload={
            "question": question[:200],
            "answer": answer.answer[:240],
            "citations": len(answer.citations),
            "gaps": answer.gaps[:6],
        },
    )
    return answer
