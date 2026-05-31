"""
Neo4j graph store for LabFoundry evidence grounding.

Provides:
- Async driver singleton with lazy initialization
- Schema constraint bootstrap (idempotent)
- Write functions (called inline from handlers, not exposed over MCP)
- Read query functions (exposed over MCP for PI/Critic agents)

The graph mirrors the research lifecycle: Findings ground Claims,
CriticVerdicts challenge Claims, and Citations link Findings to Verdicts.
"""

from __future__ import annotations

import asyncio
import logging
import os

from neo4j import AsyncDriver, AsyncGraphDatabase

log = logging.getLogger(__name__)

# =========================================================================
# Lazy singleton driver
# =========================================================================

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "boardroom")

_driver: AsyncDriver | None = None
_driver_lock = asyncio.Lock()


async def _get_driver() -> AsyncDriver:
    """Lazy-initialize the Neo4j driver on first call. Thread-safe."""
    global _driver
    async with _driver_lock:
        if _driver is None:
            _driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            log.info("neo4j: driver initialized at %s", NEO4J_URI)
    return _driver


# =========================================================================
# Schema bootstrap
# =========================================================================


async def ensure_constraints() -> None:
    """Idempotent: create constraints and indexes for the graph schema."""
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run("CREATE CONSTRAINT claim_id IF NOT EXISTS FOR (c:Claim) REQUIRE c.id IS UNIQUE")
        await session.run("CREATE CONSTRAINT finding_id IF NOT EXISTS FOR (f:Finding) REQUIRE f.id IS UNIQUE")
        await session.run("CREATE CONSTRAINT verdict_id IF NOT EXISTS FOR (v:CriticVerdict) REQUIRE v.id IS UNIQUE")
        await session.run("CREATE INDEX finding_claim_id IF NOT EXISTS FOR (f:Finding) ON (f.claim_id)")
        log.info("neo4j: constraints and indexes created")


async def ensure_corpus_constraints() -> None:
    """
    Idempotent: create constraints and indexes for the Librarian corpus nodes
    (Paper, Dataset, Source, Author). Kept separate from ensure_constraints()
    so the schema bootstrap stays modular rather than one mega-function.
    """
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run("CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE")
        await session.run("CREATE CONSTRAINT dataset_id IF NOT EXISTS FOR (d:Dataset) REQUIRE d.id IS UNIQUE")
        await session.run("CREATE CONSTRAINT source_url IF NOT EXISTS FOR (s:Source) REQUIRE s.url IS UNIQUE")
        await session.run("CREATE CONSTRAINT author_name IF NOT EXISTS FOR (a:Author) REQUIRE a.name IS UNIQUE")
        await session.run("CREATE INDEX paper_doi IF NOT EXISTS FOR (p:Paper) ON (p.doi)")
        log.info("neo4j: corpus constraints and indexes created")


# =========================================================================
# Write functions (internal, never exposed over MCP)
# =========================================================================


async def merge_claim(id: int, statement: str, status: str, confidence: float) -> None:
    """MERGE a Claim node. Idempotent."""
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run(
            """
            MERGE (c:Claim {id: $id})
            ON CREATE SET c.statement = $statement, c.status = $status, c.confidence = $confidence
            ON MATCH SET  c.status = $status, c.confidence = $confidence
            """,
            id=id,
            statement=statement,
            status=status,
            confidence=confidence,
        )


async def merge_finding_grounds_claim(
    finding_id: int,
    claim_id: int,
    source: str,
    url: str | None,
    title: str | None,
    summary: str,
    relevance_score: float,
    supports_claim: bool | None,
    audit_verdict: str | None,
    created_at: str,
) -> None:
    """
    MERGE a Finding node and the Claim it grounds.
    Create a [:GROUNDS] edge from Finding to Claim.
    Defensive: ensures Claim node exists even if claim.created was missed.
    """
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run(
            """
            MERGE (f:Finding {id: $finding_id})
            ON CREATE SET
              f.source = $source, f.url = $url, f.title = $title,
              f.summary = $summary, f.relevance_score = $relevance_score,
              f.supports_claim = $supports_claim, f.claim_id = $claim_id,
              f.created_at = $created_at
            ON MATCH SET
              f.audit_verdict = $audit_verdict

            MERGE (c:Claim {id: $claim_id})

            MERGE (f)-[r:GROUNDS]->(c)
            ON CREATE SET r.audit_verdict = $audit_verdict, r.created_at = $created_at
            ON MATCH SET  r.audit_verdict = $audit_verdict
            """,
            finding_id=finding_id,
            claim_id=claim_id,
            source=source,
            url=url,
            title=title,
            summary=summary,
            relevance_score=relevance_score,
            supports_claim=supports_claim,
            audit_verdict=audit_verdict,
            created_at=created_at,
        )


async def merge_critic_verdict_challenged_claim(
    verdict_id: int,
    claim_id: int,
    verdict: str,
    confidence: float,
    reasoning: str,
    action: str,
    cited_finding_ids: list[int],
    created_at: str,
) -> None:
    """
    MERGE a CriticVerdict node and create [:CHALLENGED] edge to the Claim.
    Create [:CITED_BY] edges from each Finding cited in the verdict.
    """
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run(
            """
            MERGE (v:CriticVerdict {id: $verdict_id})
            ON CREATE SET
              v.verdict = $verdict, v.confidence = $confidence,
              v.reasoning = $reasoning, v.action = $action,
              v.created_at = $created_at

            MERGE (c:Claim {id: $claim_id})

            MERGE (v)-[r:CHALLENGED]->(c)
            ON CREATE SET r.action = $action, r.confidence = $confidence, r.created_at = $created_at
            ON MATCH SET  r.action = $action, r.confidence = $confidence
            """,
            verdict_id=verdict_id,
            claim_id=claim_id,
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            action=action,
            created_at=created_at,
        )

        # Create [:CITED_BY] edges from each cited finding to the verdict.
        if cited_finding_ids:
            await session.run(
                """
                UNWIND $cited_ids AS fid
                MERGE (f:Finding {id: fid})
                MERGE (v:CriticVerdict {id: $verdict_id})
                MERGE (f)-[:CITED_BY {created_at: $created_at}]->(v)
                """,
                cited_ids=cited_finding_ids,
                verdict_id=verdict_id,
                created_at=created_at,
            )


# -------------------------------------------------------------------------
# Corpus write functions (Librarian — Phase 2). Internal, never over MCP.
# Paper.id == documents.id (the graph's existing surrogate-identity convention).
# Best-effort: a missed write degrades query quality but never blocks ingest.
# -------------------------------------------------------------------------


async def merge_paper(
    id: int,
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
    year: int | None = None,
    trust_tier: str | None = None,
    source_url: str | None = None,
    authors: list[str] | None = None,
) -> None:
    """
    MERGE a Paper node and its provenance edges. Idempotent.
    If source_url is given, MERGE (s:Source) + (p)-[:FROM]->(s).
    For each author name, MERGE (a:Author) + (p)-[:BY]->(a).
    """
    try:
        driver = await _get_driver()
        async with driver.session() as session:
            await session.run(
                """
                MERGE (p:Paper {id: $id})
                SET p.doi = $doi, p.arxiv_id = $arxiv_id, p.title = $title,
                    p.year = $year, p.trust_tier = $trust_tier
                """,
                id=id,
                doi=doi,
                arxiv_id=arxiv_id,
                title=title,
                year=year,
                trust_tier=trust_tier,
            )

            if source_url:
                await session.run(
                    """
                    MERGE (p:Paper {id: $id})
                    MERGE (s:Source {url: $source_url})
                    MERGE (p)-[:FROM]->(s)
                    """,
                    id=id,
                    source_url=source_url,
                )

            if authors:
                await session.run(
                    """
                    MERGE (p:Paper {id: $id})
                    WITH p
                    UNWIND $authors AS author_name
                    MERGE (a:Author {name: author_name})
                    MERGE (p)-[:BY]->(a)
                    """,
                    id=id,
                    authors=authors,
                )
    except Exception:
        log.exception("neo4j: merge_paper failed for paper %s — continuing", id)


async def merge_dataset(
    id: int,
    *,
    name: str | None = None,
    modality: str | None = None,
    task: str | None = None,
) -> None:
    """MERGE a Dataset node + props. Idempotent."""
    try:
        driver = await _get_driver()
        async with driver.session() as session:
            await session.run(
                """
                MERGE (d:Dataset {id: $id})
                SET d.name = $name, d.modality = $modality, d.task = $task
                """,
                id=id,
                name=name,
                modality=modality,
                task=task,
            )
    except Exception:
        log.exception("neo4j: merge_dataset failed for dataset %s — continuing", id)


async def link_finding_cites_paper(
    finding_id: int,
    paper_id: int,
    created_at: str | None = None,
) -> None:
    """
    MERGE (f:Finding)-[:CITES]->(p:Paper). Defensive MERGE on both endpoints
    so it is order-independent (like the existing finding/claim merges).
    """
    try:
        driver = await _get_driver()
        async with driver.session() as session:
            await session.run(
                """
                MERGE (f:Finding {id: $finding_id})
                MERGE (p:Paper {id: $paper_id})
                MERGE (f)-[r:CITES]->(p)
                ON CREATE SET r.created_at = $created_at
                """,
                finding_id=finding_id,
                paper_id=paper_id,
                created_at=created_at,
            )
    except Exception:
        log.exception(
            "neo4j: link_finding_cites_paper failed for finding %s -> paper %s — continuing",
            finding_id,
            paper_id,
        )


# =========================================================================
# Read query functions (exposed over MCP)
# =========================================================================


async def get_claim_evidence_chain(claim_id: int, limit: int = 20) -> list[dict]:
    """
    Return all Findings that GROUND a claim, ordered by relevance_score DESC.
    Answers: "What evidence supports this claim?"
    """
    driver = await _get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (f:Finding)-[:GROUNDS]->(c:Claim {id: $claim_id})
            RETURN
              f.id AS finding_id,
              f.source AS source,
              f.url AS url,
              f.title AS title,
              f.summary AS summary,
              f.relevance_score AS relevance_score,
              f.supports_claim AS supports_claim,
              r.audit_verdict AS audit_verdict
            ORDER BY f.relevance_score DESC
            LIMIT $limit
            """,
            claim_id=claim_id,
            limit=limit,
        )
        return [dict(record) for record in await result.data()]


async def get_claim_critics(claim_id: int) -> list[dict]:
    """
    Return all CriticVerdicts that CHALLENGED a claim.
    Include the findings cited in each verdict.
    Answers: "Who challenged this claim and why?"
    """
    driver = await _get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (v:CriticVerdict)-[:CHALLENGED]->(c:Claim {id: $claim_id})
            OPTIONAL MATCH (f:Finding)-[:CITED_BY]->(v)
            RETURN
              v.id AS verdict_id,
              v.verdict AS verdict,
              v.confidence AS confidence,
              v.reasoning AS reasoning,
              v.action AS action,
              v.created_at AS created_at,
              collect(f.id) AS cited_finding_ids
            ORDER BY v.created_at DESC
            """,
            claim_id=claim_id,
        )
        return [dict(record) for record in await result.data()]


async def get_finding_influence(finding_id: int) -> dict:
    """
    Return the claim this finding grounds, plus any CriticVerdicts that cited it.
    Multi-hop: Finding -> Claim (via GROUNDS) + Finding -> CriticVerdict (via CITED_BY)
    Answers: "What impact did this finding have?"
    """
    driver = await _get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (f:Finding {id: $finding_id})
            OPTIONAL MATCH (f)-[:GROUNDS]->(c:Claim)
            OPTIONAL MATCH (f)-[:CITED_BY]->(v:CriticVerdict)
            RETURN
              f.id AS finding_id,
              f.source AS source,
              f.url AS url,
              f.title AS title,
              f.summary AS summary,
              f.relevance_score AS relevance_score,
              c.id AS claim_id,
              c.statement AS claim_statement,
              collect(DISTINCT v.id) AS cited_by_verdict_ids,
              collect(DISTINCT {id: v.id, verdict: v.verdict, action: v.action}) AS verdicts
            """,
            finding_id=finding_id,
        )
        records = await result.data()
        if records:
            return dict(records[0])
        return {"finding_id": finding_id, "not_found": True}
