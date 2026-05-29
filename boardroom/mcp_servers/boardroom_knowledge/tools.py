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
from typing import Any, Optional

from neo4j import AsyncDriver, AsyncGraphDatabase

log = logging.getLogger(__name__)

# =========================================================================
# Lazy singleton driver
# =========================================================================

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "boardroom")

_driver: Optional[AsyncDriver] = None
_driver_lock = asyncio.Lock()


async def _get_driver() -> AsyncDriver:
    """Lazy-initialize the Neo4j driver on first call. Thread-safe."""
    global _driver
    async with _driver_lock:
        if _driver is None:
            _driver = AsyncGraphDatabase.driver(
                NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
            )
            log.info("neo4j: driver initialized at %s", NEO4J_URI)
    return _driver


# =========================================================================
# Schema bootstrap
# =========================================================================


async def ensure_constraints() -> None:
    """Idempotent: create constraints and indexes for the graph schema."""
    driver = await _get_driver()
    async with driver.session() as session:
        await session.run(
            "CREATE CONSTRAINT claim_id IF NOT EXISTS FOR (c:Claim) REQUIRE c.id IS UNIQUE"
        )
        await session.run(
            "CREATE CONSTRAINT finding_id IF NOT EXISTS FOR (f:Finding) REQUIRE f.id IS UNIQUE"
        )
        await session.run(
            "CREATE CONSTRAINT verdict_id IF NOT EXISTS FOR (v:CriticVerdict) REQUIRE v.id IS UNIQUE"
        )
        await session.run(
            "CREATE INDEX finding_claim_id IF NOT EXISTS FOR (f:Finding) ON (f.claim_id)"
        )
        log.info("neo4j: constraints and indexes created")


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
    url: Optional[str],
    title: Optional[str],
    summary: str,
    relevance_score: float,
    supports_claim: Optional[bool],
    audit_verdict: Optional[str],
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
