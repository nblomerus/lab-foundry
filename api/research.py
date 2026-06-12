"""
Research arc API — the dossiers behind the /research page.

GET /research/dossiers       — per direction: arc stage status (topic → review →
                               proposal → experiments → finding → article)
GET /research/documents/{id} — one document, full markdown body
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research"])


def _obj(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


@router.get("/dossiers")
async def dossiers(request: Request, limit: int = 24) -> dict:
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.statement, c.status, c.confidence, dg.status AS gate,
                   (SELECT count(*) FROM experiment_runs e JOIN tasks t ON t.id = e.task_id
                      WHERE t.claim_id = c.id AND e.status = 'completed') AS experiments_done,
                   (SELECT rf.supported FROM research_findings rf
                      WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS finding_supported,
                   (SELECT rf.confidence FROM research_findings rf
                      WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS finding_confidence
            FROM claims c
            LEFT JOIN direction_gate dg ON dg.claim_id = c.id
            WHERE c.claim_kind = 'direction'
              AND (c.status IN ('proposed','tested','weakly_supported','replicated','concluded')
                   OR EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id))
            ORDER BY c.id DESC LIMIT $1
            """,
            limit,
        )
        ids = [r["id"] for r in rows]
        docs = (
            await conn.fetch(
                "SELECT id, claim_id, kind, title, created_at FROM research_documents "
                "WHERE claim_id = ANY($1) AND status = 'final' ORDER BY id",
                ids,
            )
            if ids
            else []
        )
    docs_by_claim: dict[int, dict] = {}
    for d in docs:
        docs_by_claim.setdefault(d["claim_id"], {})[d["kind"]] = {
            "id": d["id"],
            "title": d["title"],
            "at": d["created_at"].isoformat(),
        }
    out = []
    for r in rows:
        dd = docs_by_claim.get(r["id"], {})
        out.append(
            {
                "claim_id": r["id"],
                "statement": r["statement"],
                "status": r["status"],
                "gate": r["gate"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "experiments_done": r["experiments_done"],
                "finding_supported": r["finding_supported"],
                "finding_confidence": float(r["finding_confidence"]) if r["finding_confidence"] is not None else None,
                "documents": dd,
            }
        )
    return {"dossiers": out}


@router.get("/documents/{doc_id}")
async def document(doc_id: int, request: Request) -> dict:
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT rd.id, rd.claim_id, rd.kind, rd.title, rd.body_md, rd.meta, rd.citations, rd.status, "
            "rd.created_at, c.statement AS direction "
            "FROM research_documents rd JOIN claims c ON c.id = rd.claim_id WHERE rd.id = $1",
            doc_id,
        )
    if r is None:
        return {"error": "not found", "id": doc_id}
    return {
        "id": r["id"],
        "claim_id": r["claim_id"],
        "direction": r["direction"],
        "kind": r["kind"],
        "title": r["title"],
        "body_md": r["body_md"],
        "meta": _obj(r["meta"], {}),
        "citations": _obj(r["citations"], []),
        "status": r["status"],
        "created_at": r["created_at"].isoformat(),
    }
