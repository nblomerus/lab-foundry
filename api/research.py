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
                   dsv.stage, dsv.blocker, dsv.experiments_done,
                   (SELECT rf.supported FROM research_findings rf
                      WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS finding_supported,
                   (SELECT rf.confidence FROM research_findings rf
                      WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS finding_confidence,
                   (SELECT rf.data_realism FROM research_findings rf
                      WHERE rf.direction_claim_id = c.id ORDER BY rf.id DESC LIMIT 1) AS finding_realism
            FROM claims c
            LEFT JOIN direction_gate dg ON dg.claim_id = c.id
            LEFT JOIN direction_stage_v dsv ON dsv.claim_id = c.id
            WHERE c.claim_kind = 'direction'
              AND (c.status IN ('proposed','tested','weakly_supported','replicated','concluded')
                   OR EXISTS (SELECT 1 FROM research_documents rd WHERE rd.claim_id = c.id))
            ORDER BY c.id DESC LIMIT $1
            """,
            limit,
        )
        ids = [r["id"] for r in rows]
        # LEFT JOIN the corpus doc (linked by the scholarship canonical_key) so the UI can show
        # whether each artifact has actually landed queryable in Mimir — the "make sure Mimir has
        # every research artifact" signal, surfaced per document.
        docs = (
            await conn.fetch(
                "SELECT rd.id, rd.claim_id, rd.kind, rd.title, rd.created_at, "
                "COALESCE(d.queryable, false) AS in_mimir "
                "FROM research_documents rd "
                "LEFT JOIN documents d ON d.canonical_key = "
                "  'scholarship:' || rd.kind || ':claim:' || rd.claim_id || ':doc:' || rd.id "
                "WHERE rd.claim_id = ANY($1) AND rd.status = 'final' ORDER BY rd.id",
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
            "in_mimir": d["in_mimir"],
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
                "stage": r["stage"],  # the single derived stage (direction_stage_v) — the UI no longer re-derives
                "blocker": r["blocker"],  # the one reason it's parked, if any (e.g. 'held by adjudicator')
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "experiments_done": r["experiments_done"],
                "finding_supported": r["finding_supported"],
                "finding_confidence": float(r["finding_confidence"]) if r["finding_confidence"] is not None else None,
                "finding_realism": r["finding_realism"],
                "documents": dd,
            }
        )
    return {"dossiers": out}


@router.get("/documents")
async def documents(request: Request, limit: int = 200, include_superseded: bool = False) -> dict:
    """Every research document the lab has written — the full library to browse, newest first.
    One flat list across all directions (lit reviews, proposals, articles), each linked to its
    corpus doc so the UI can show whether Mimir carries it."""
    pool = request.app.state.pool
    status_filter = "" if include_superseded else "AND rd.status = 'final'"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT rd.id, rd.claim_id, rd.kind, rd.title, rd.status, rd.created_at,
                   c.statement AS direction,
                   COALESCE(d.queryable, false) AS in_mimir
            FROM research_documents rd
            JOIN claims c ON c.id = rd.claim_id
            LEFT JOIN documents d ON d.canonical_key =
              'scholarship:' || rd.kind || ':claim:' || rd.claim_id || ':doc:' || rd.id
            WHERE TRUE {status_filter}
            ORDER BY rd.created_at DESC, rd.id DESC
            LIMIT $1
            """,
            limit,
        )
    return {
        "documents": [
            {
                "id": r["id"],
                "claim_id": r["claim_id"],
                "direction": r["direction"],
                "kind": r["kind"],
                "title": r["title"],
                "status": r["status"],
                "in_mimir": r["in_mimir"],
                "at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


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
        # in_mimir: has this artifact landed queryable in the Library? versions: how many times
        # the lab has rewritten this kind for the direction (this final + prior superseded).
        in_mimir = await conn.fetchval(
            "SELECT COALESCE(queryable, false) FROM documents WHERE canonical_key = $1",
            f"scholarship:{r['kind']}:claim:{r['claim_id']}:doc:{r['id']}",
        )
        versions = await conn.fetchval(
            "SELECT count(*) FROM research_documents WHERE claim_id = $1 AND kind = $2",
            r["claim_id"],
            r["kind"],
        )
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
        "in_mimir": bool(in_mimir),
        "versions": versions or 1,
    }
