"""
The FIELD MODEL — Ariadne's Domain-Expert read on the AI/ML research landscape.

Built from the context graph (concept nodes METHOD/TASK/DATASET + their Paper edges),
it classifies every prominent concept by TWO orthogonal signals:

  * prominence — all-time `count(DISTINCT paper)` (the saturation axis), and
  * velocity   — SHARE-normalized growth across the last two paper cohorts. Papers carry
                 a date only via their arxiv_id (YYMM); the dated corpus concentrates in
                 the two latest cohorts, and raw counts inflate with corpus growth, so a
                 concept counts as "rising" only if its SHARE of recent papers grew:
                 velocity = recent_share / prior_share − 1.

  trend_state ∈ emerging | hot | stable | saturated | declining

`build_field_model` recomputes the whole table from Neo4j (re-derivable, cheap to rebuild);
`read_field_brief` renders it as the grounding block Ariadne reads when she deliberates.
"""

from __future__ import annotations

import logging
from collections import Counter

from neo4j import AsyncDriver

log = logging.getLogger(__name__)

KIND_RELS = {"METHOD": "USES", "TASK": "ADDRESSES", "DATASET": "EVALUATED_ON"}
MIN_TOTAL = 3  # ignore hapax concepts — the landscape is about footholds, not one-offs
SAT_PERCENTILE = 0.90  # "saturated" = prominence in the top decile and flat


async def _windows(driver: AsyncDriver) -> tuple[str, str, int, int] | None:
    """The two most-populated arxiv cohorts (YYMM), as (recent, prior, n_recent, n_prior)."""
    async with driver.session() as s:
        res = await s.run(
            "MATCH (p:Paper) WHERE p.arxiv_id IS NOT NULL "
            "RETURN left(p.arxiv_id, 4) AS ym, count(DISTINCT p) AS n ORDER BY n DESC LIMIT 6"
        )
        rows = [(r["ym"], r["n"]) async for r in res]
    if len(rows) < 2:
        return None
    (recent_ym, n_recent), (prior_ym, n_prior) = sorted(rows[:2], key=lambda r: r[0], reverse=True)
    return recent_ym, prior_ym, n_recent, n_prior


async def _concepts(driver: AsyncDriver, label: str, rel: str, recent: str, prior: str) -> list[tuple]:
    """Per concept of `label`: (key, name, total, recent_n, prior_n)."""
    q = (
        f"MATCH (n:{label})<-[:{rel}]-(p:Paper) "
        f"WITH n, count(DISTINCT p) AS total, "
        f"     count(DISTINCT CASE WHEN left(p.arxiv_id,4) = $recent THEN p END) AS recent_n, "
        f"     count(DISTINCT CASE WHEN left(p.arxiv_id,4) = $prior  THEN p END) AS prior_n "
        f"RETURN n.key AS key, n.name AS name, total, recent_n, prior_n"
    )
    async with driver.session() as s:
        res = await s.run(q, recent=recent, prior=prior)
        return [(r["key"], r["name"], r["total"], r["recent_n"], r["prior_n"]) async for r in res]


def _classify(
    total: int, recent_n: int, prior_n: int, n_recent: int, n_prior: int, sat_threshold: int
) -> tuple[str, float]:
    """(trend_state, velocity) from prominence + share-normalized growth."""
    rs = recent_n / n_recent if n_recent else 0.0
    ps = prior_n / n_prior if n_prior else 0.0
    if ps > 0:
        velocity = rs / ps - 1.0
    elif rs > 0:
        velocity = 1.0  # appeared from nothing
    else:
        velocity = 0.0

    if prior_n <= 2 and recent_n >= 5 and total < sat_threshold:
        state = "emerging"  # genuinely new — little base AND not already prominent all-time
    elif recent_n >= 5 and velocity >= 0.25:
        state = "hot"  # established and gaining share (incl. prominent fields resurging)
    elif prior_n >= 3 and velocity <= -0.40:
        state = "declining"  # shrinking from a real base
    elif total >= sat_threshold and -0.25 < velocity < 0.25:
        state = "saturated"  # lots of all-time work, flat share — well-trodden
    else:
        state = "stable"
    return state, round(velocity, 3)


async def build_field_model(driver: AsyncDriver, pool) -> dict:
    """Recompute the field model from the graph and replace the Postgres table wholesale."""
    w = await _windows(driver)
    if not w:
        raise RuntimeError("field model: graph has < 2 dated paper cohorts — nothing to trend")
    recent_ym, prior_ym, n_recent, n_prior = w

    gathered: list[tuple] = []  # (label, key, name, total, recent_n, prior_n)
    for label, rel in KIND_RELS.items():
        for key, name, total, recent_n, prior_n in await _concepts(driver, label, rel, recent_ym, prior_ym):
            if total >= MIN_TOTAL and key and name:
                gathered.append((label, key, name, total, recent_n, prior_n))

    totals = sorted(g[3] for g in gathered)
    sat_threshold = totals[int(SAT_PERCENTILE * len(totals))] if totals else 1 << 30

    rows: list[tuple] = []
    for label, key, name, total, recent_n, prior_n in gathered:
        state, velocity = _classify(total, recent_n, prior_n, n_recent, n_prior, sat_threshold)
        rows.append((label, key, name, total, recent_n, prior_n, velocity, state, recent_ym, prior_ym))

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM field_model")
        await conn.executemany(
            "INSERT INTO field_model (concept_kind, concept_key, concept_name, total_papers, "
            "recent_papers, prior_papers, velocity, trend_state, recent_window, prior_window) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            rows,
        )

    return {
        "recent": recent_ym,
        "prior": prior_ym,
        "n_recent": n_recent,
        "n_prior": n_prior,
        "concepts": len(rows),
        "sat_threshold": sat_threshold,
        "by_state": dict(Counter(r[7] for r in rows)),
    }


async def read_field_brief(pool, per_state: int = 8) -> str:
    """Render the field model as the grounding block Ariadne reads. Empty string if unbuilt."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT concept_name, total_papers, velocity, trend_state, recent_window, prior_window "
            "FROM field_model WHERE trend_state IN ('hot','emerging','saturated','declining') "
            "ORDER BY trend_state, "
            "CASE WHEN trend_state = 'emerging' THEN recent_papers ELSE total_papers END DESC"
        )
    if not rows:
        return ""
    rw, pw = rows[0]["recent_window"], rows[0]["prior_window"]
    buckets: dict[str, list[str]] = {"hot": [], "emerging": [], "saturated": [], "declining": []}
    for r in rows:
        b = r["trend_state"]
        if len(buckets[b]) < per_state:
            sign = "+" if r["velocity"] >= 0 else ""
            buckets[b].append(f"{r['concept_name']} ({r['total_papers']}p, {sign}{int(r['velocity'] * 100)}%)")
    labels = {
        "hot": "HOT (active, gaining share)",
        "emerging": "EMERGING (new, gaining)",
        "saturated": "SATURATED (well-trodden — differentiate or avoid)",
        "declining": "DECLINING (cooling)",
    }
    out = [f"## Field model — AI/ML landscape (context graph; trend = share shift {pw}→{rw}, %=relative share change)"]
    for b in ("hot", "emerging", "saturated", "declining"):
        if buckets[b]:
            out.append(f"{labels[b]}: " + "; ".join(buckets[b]))
    return "\n".join(out)
