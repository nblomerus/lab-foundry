"""
First light for Mimir — the Library's Warden, and the lab's first living agent.

A focused, BOUNDED, observable driver that exercises Mimir's real handler code
against the live stack and then reads the DB back to report exactly what he did.
Unlike `python -m harness.main` (which boots the whole company behind a 6-hour
sweep timer and a bootstrap gate), this runs ONE Mimir cycle on demand, so you
can watch the trust-gated ingest path work end-to-end in seconds:

    discover  run the real scouts over a few topics -> emit source.discovered ->
              stage -> classify_trust -> (LLM tie-breaker if web_unknown) ->
              certify/quarantine -> embed -> queryable.   [the headline]
    seed      ingest ONE known source (arXiv id, or a URL — a URL is the easy way
              to hit the web_unknown boundary and fire the live LLM tie-breaker).
    acquire   drive the DEMAND path: request_acquire -> adjudicate -> reply.

Everything is the SAME code the dispatcher calls in production — real network
fetch, real Ollama embed, real premium chain — only the trigger and the bounds
differ. Nothing is mocked. New documents land in the real corpus (that is the
point); re-runs dedupe at stage time, so it is safe to run repeatedly.

Usage:
    python -m ops.mimir_firstlight                          # discover (default)
    python -m ops.mimir_firstlight --topic "mixture of experts" --per-topic 2
    python -m ops.mimir_firstlight --mode seed --arxiv-id 1706.03762
    python -m ops.mimir_firstlight --mode seed --url https://some-ml-blog/post
    python -m ops.mimir_firstlight --mode acquire --query "speculative decoding"
    python -m ops.mimir_firstlight --no-llm                 # deterministic only

Env (auto-loaded from .env if present):
    DATABASE_URL   required
    OLLAMA_URL     default http://localhost:11434   (embedder + local fallback)
    EMBED_MODEL    default nomic-embed-text          (must be pulled in Ollama)
    LIBRARY_SCOUTS default "arxiv"                    (comma-sep: arxiv,web,github)
    plus the cloud/premium keys (DEEPSEEK_API_KEY, ...) for the LLM tie-breaker.

Exit code: 0 = cycle ran (see the report), 2 = a hard preflight check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Mimir's handlers gate on MIMIR_LOOP; this runner IS Mimir's loop for one cycle,
# so turn the gate on before anything imports it. (handle_acquire_requested reads
# it at call time, so setting it here is sufficient.)
os.environ.setdefault("MIMIR_LOOP", "on")


# -------------------------------------------------------------------------
# .env loader — no dependency (python-dotenv is optional; the Makefile injects
# .env for `make` targets, but `python -m ops.mimir_firstlight` runs bare).
# -------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Best-effort: set vars from ./.env that aren't already in the environment."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# -------------------------------------------------------------------------
# Small report helpers
# -------------------------------------------------------------------------

_OK = "✓"  # ✓
_NO = "✗"  # ✗
_DOT = "•"  # •


def _line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")


async def _register_vector_codec(conn) -> None:
    """The corpus writes vector(768) via state.set_chunk_embeddings; asyncpg needs
    the pgvector codec registered per connection (mirrors harness/main.py)."""
    try:
        import pgvector.asyncpg

        await pgvector.asyncpg.register_vector(conn)
    except Exception as e:  # noqa: BLE001 — without it, embed writes fail, not stage
        print(f"  {_NO} pgvector codec not registered (embed writes will fail): {e}", file=sys.stderr)


# -------------------------------------------------------------------------
# Preflight — fail fast with a clear ✓/✗ per dependency
# -------------------------------------------------------------------------


async def _preflight(pool, *, mode: str, want_llm: bool) -> bool:
    """Probe every live dependency Mimir's cycle touches. Returns True if all the
    HARD checks pass; soft checks only warn."""
    import httpx

    print("Preflight")
    hard_ok = True

    # 1. DB schema (HARD)
    try:
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename IN ('documents','chunks','certifications','claims')"
            )
        present = {r["tablename"] for r in tables}
        missing = {"documents", "chunks", "certifications", "claims"} - present
        if missing:
            _line(_NO, f"DB schema — missing tables: {sorted(missing)} (run `make migrate`)")
            hard_ok = False
        else:
            _line(_OK, "DB schema — documents/chunks/certifications/claims present")
    except Exception as e:  # noqa: BLE001
        _line(_NO, f"DB unreachable: {e}")
        return False

    # company_state (SOFT — Mimir doesn't need it; the full harness does)
    async with pool.acquire() as conn:
        seeded = await conn.fetchval("SELECT count(*) FROM company_state WHERE id = 1")
    _line(
        _OK if seeded else _DOT,
        "company_state seeded"
        if seeded
        else "company_state NOT seeded — fine for Mimir; run `python -m ops.bootstrap` before the full harness",
    )

    # 2. Ollama + embed model (HARD — embed_and_finalize needs it to make docs queryable)
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    embed_model = os.environ.get("EMBED_MODEL", "nomic-embed-text")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            tags = (await c.get(f"{ollama_url}/api/tags")).json()
        names = [m.get("name", "") for m in tags.get("models", [])]
        if any(n == embed_model or n.startswith(f"{embed_model}:") for n in names):
            _line(_OK, f"Ollama embedder — {embed_model} available at {ollama_url}")
        else:
            _line(_NO, f"Ollama up but embed model {embed_model!r} not pulled (`ollama pull {embed_model}`)")
            hard_ok = False
    except Exception as e:  # noqa: BLE001
        _line(_NO, f"Ollama unreachable at {ollama_url}: {e}")
        hard_ok = False

    # 3. arXiv reachable — HARD for discover (the default scout), soft otherwise
    scouts = os.environ.get("LIBRARY_SCOUTS", "arxiv")
    if mode == "discover" and "arxiv" in scouts:
        try:
            from library.ingest.fetcher import search_arxiv

            hits = await search_arxiv("large language models", max_results=1)
            if hits:
                _line(_OK, "arXiv API reachable")
            else:
                _line(_NO, "arXiv API returned no results (network / rate-limit?)")
                hard_ok = False
        except Exception as e:  # noqa: BLE001
            _line(_NO, f"arXiv API unreachable: {e}")
            hard_ok = False

    # 4. SearXNG (SOFT — only used by the web scout)
    if "web" in scouts:
        searx = os.environ.get("SEARXNG_URL")
        ok = False
        if searx:
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    ok = (await c.get(searx)).status_code < 500
            except Exception:  # noqa: BLE001
                ok = False
        _line(_OK if ok else _DOT, f"SearXNG {'reachable' if ok else 'unreachable — web scout will no-op'}")

    # 5. Premium chain (SOFT — the LLM tie-breaker only fires for web_unknown)
    if want_llm:
        from harness.router import build_premium_chain

        chain = build_premium_chain(os.environ)
        if chain:
            lead = chain[0]
            _line(_OK, f"LLM tie-breaker ready — WORKHORSE leads with {lead.provider.value}:{lead.model_name}")
        else:
            _line(_DOT, "no premium chain — tie-breaker will fall back to local Ollama (still works)")

    print()
    return hard_ok


# -------------------------------------------------------------------------
# Drivers — each runs the real handler code; the report reads the DB back
# -------------------------------------------------------------------------


async def _ingest_and_render(source: dict, state, router, curator) -> dict | None:
    """Run the exact ingest core the dispatcher calls, print a one-line verdict."""
    from agents.mimir.handler import ingest_source

    res = await ingest_source(source, state, router=router, curator=curator, session=None)
    decision = res.get("decision")
    if decision == "approve":
        _line(
            _OK,
            f"doc {res['document_id']} APPROVED  tier={res.get('tier')}  "
            f"llm={res.get('used_llm', False)}  "
            f"embedded={res.get('embedded')}  queryable={res.get('queryable')}",
        )
    elif decision == "block":
        _line(_NO, f"doc {res['document_id']} BLOCKED  llm={res.get('used_llm', False)}  — {res.get('reason')}")
    else:
        _line(_DOT, f"skipped — {res.get('reason') or res.get('deduped') and 'deduped' or res}")
    return res


async def _drive_discover(pool, state, router, curator, *, topics, per_topic, limit) -> None:
    """Run a real discovery sweep, then ingest the freshly discovered sources —
    the same two-step the watchdog + dispatcher do, compressed into one pass."""
    from agents.mimir.collectors import run_discovery_sweep

    print(f"Discovery sweep  (topics={topics or 'agenda+frontier'}  per_topic={per_topic})")
    sweep = await run_discovery_sweep(topics, state, per_topic=per_topic)
    _line(_DOT, f"scanned {sweep['scanned']} sources, {sweep['discovered']} new -> source.discovered")
    print()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, payload FROM events WHERE event_type='source.discovered' "
            "AND status='pending' ORDER BY id LIMIT $1",
            limit,
        )
    if not rows:
        print("No fresh sources to ingest (all already in the corpus, or the sweep found nothing).")
        return

    print(f"Ingesting {len(rows)} discovered source(s)  (cap --limit={limit})")
    import json

    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        await _ingest_and_render(payload["source"], state, router, curator)
        # Mark consumed so a later real-harness run doesn't reprocess (it would
        # dedupe at stage time anyway — this just keeps the events table honest).
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE events SET status='consumed', consumed_at=now(), "
                "consumed_by_handler='mimir.firstlight' WHERE id=$1",
                row["id"],
            )


async def _drive_seed(state, router, curator, *, arxiv_id, url) -> None:
    """Ingest one explicit source. A URL is the easy way to land on web_unknown
    and fire the live LLM tie-breaker; an arXiv id classifies to preprint."""
    if arxiv_id:
        source = {
            "kind": "paper",
            "source_kind": "arxiv",
            "canonical_key": arxiv_id,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "arxiv_id": arxiv_id,
            "why": "first-light seed",
        }
        print(f"Seeding arXiv:{arxiv_id}")
    else:
        source = {
            "kind": "web",
            "source_kind": "web",
            "canonical_key": url,
            "url": url,
            "why": "first-light seed (web — exercises the trust tie-breaker)",
        }
        print(f"Seeding URL {url}")
    await _ingest_and_render(source, state, router, curator)


async def _drive_acquire(pool, state, router, curator, *, requester, arxiv_id, query) -> None:
    """Drive the demand path: an allowed agent asks Mimir for a source, Mimir
    adjudicates (cap -> resolve -> dedupe -> ingest) and replies."""
    from agents.mimir.acquire import AcquireRequest, handle_acquire_requested, request_acquire

    why = f"first-light acquire smoke for {requester}: needs this source to ground a specific claim"
    req = AcquireRequest(requester=requester, why=why, arxiv_id=arxiv_id, query=query)
    print(f"Acquire  requester={requester}  {'arxiv:' + arxiv_id if arxiv_id else 'query:' + repr(query)}")

    await request_acquire(state, req)  # emits acquire.requested
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT id, payload FROM events WHERE event_type='acquire.requested' "
            "AND status='pending' ORDER BY id DESC LIMIT 1"
        )
    if ev is None:
        _line(_NO, "acquire.requested was not emitted")
        return

    class _Shim:
        pass

    shim = _Shim()
    shim.state, shim.router, shim.curator, shim.session = state, router, curator, None

    import json

    payload = ev["payload"] if isinstance(ev["payload"], dict) else json.loads(ev["payload"])
    res = await handle_acquire_requested({"id": ev["id"], "payload": payload}, shim)
    status = (res or {}).get("status")
    mark = _OK if status in {"fulfilled", "already_have"} else (_NO if status == "rejected" else _DOT)
    _line(mark, f"acquire -> {status}: {(res or {}).get('reason')}  doc={(res or {}).get('document_id')}")


# -------------------------------------------------------------------------
# Corpus snapshot (the source of truth, read back after the cycle)
# -------------------------------------------------------------------------


async def _corpus_snapshot(pool) -> None:
    print()
    print("Corpus snapshot")
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM documents")
        queryable = await conn.fetchval("SELECT count(*) FROM documents WHERE queryable")
        certs = await conn.fetchval("SELECT count(*) FROM certifications")
        llm_certs = await conn.fetchval("SELECT count(*) FROM certifications WHERE used_llm")
        by_tier = await conn.fetch("SELECT trust_tier, count(*) AS n FROM documents GROUP BY trust_tier ORDER BY n DESC")
    _line(_DOT, f"documents: {total} total, {queryable} queryable")
    _line(_DOT, f"certifications: {certs} total ({llm_certs} used the LLM tie-breaker)")
    for r in by_tier:
        _line(_DOT, f"  tier {r['trust_tier']}: {r['n']}")


# -------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    _load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set (and no .env) — cannot run.", file=sys.stderr)
        return 2

    import asyncpg

    from harness.curator import Curator
    from harness.router import GPULock, Router, build_cloud_chain, build_premium_chain
    from memory.client import ZepClient
    from skills.client import LessonsClient
    from state.client import PostgresClient

    want_llm = not args.no_llm
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=6, init=_register_vector_codec)

    router = None
    created_temp_state = False
    try:
        if not await _preflight(pool, mode=args.mode, want_llm=want_llm):
            print(f"{_NO} Hard preflight check failed — aborting before any ingest.", file=sys.stderr)
            return 2

        # The LLM tie-breaker goes through the Curator, which injects the company
        # constitution + phase layers (both read company_state) into EVERY build.
        # If the company isn't bootstrapped yet, seed a TEMPORARY minimal row so
        # the tie-breaker can run, and drop it afterwards — so we never block a
        # later real `ops.bootstrap` (which refuses if company_state exists).
        if want_llm:
            async with pool.acquire() as conn:
                if not await conn.fetchval("SELECT 1 FROM company_state WHERE id = 1"):
                    await conn.execute(
                        "INSERT INTO company_state (id, problem_statement, deadline) "
                        "VALUES (1, 'first-light: temporary state for the Mimir LLM tie-breaker test', "
                        "now() + interval '30 days') ON CONFLICT (id) DO NOTHING"
                    )
                    created_temp_state = True
                    _line(_DOT, "seeded a TEMPORARY company_state for the tie-breaker (removed on exit)")

        state = PostgresClient(pool=pool)
        curator = router = None
        if want_llm:
            # Built exactly as harness/main.py does, so the tie-breaker uses the
            # real premium chain. Mimir reads router/curator via getattr, so the
            # deterministic path is unaffected when --no-llm omits them.
            curator = Curator(state=state, memory=ZepClient.from_env(), lessons=LessonsClient(pool=pool))
            router = Router(
                pool=pool,
                gpu_lock=GPULock(),
                ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                cloud_chain=build_cloud_chain(os.environ),
                premium_chain=build_premium_chain(os.environ),
            )

        if args.mode == "discover":
            await _drive_discover(
                pool,
                state,
                router,
                curator,
                topics=args.topic or None,
                per_topic=args.per_topic,
                limit=args.limit,
            )
        elif args.mode == "seed":
            if not (args.arxiv_id or args.url):
                print("--mode seed needs --arxiv-id or --url", file=sys.stderr)
                return 2
            await _drive_seed(state, router, curator, arxiv_id=args.arxiv_id, url=args.url)
        elif args.mode == "acquire":
            if not (args.arxiv_id or args.query):
                print("--mode acquire needs --arxiv-id or --query", file=sys.stderr)
                return 2
            await _drive_acquire(
                pool,
                state,
                router,
                curator,
                requester=args.requester,
                arxiv_id=args.arxiv_id,
                query=args.query,
            )

        await _corpus_snapshot(pool)
        print()
        print(f"{_OK} Mimir cycle complete. Run him continuously with: MIMIR_LOOP=on python -m harness.main")
        return 0
    finally:
        if router is not None:
            await router.close()
        if created_temp_state:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM company_state WHERE id = 1")
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ops.mimir_firstlight", description="First light for Mimir.")
    p.add_argument("--mode", choices=["discover", "seed", "acquire"], default="discover")
    p.add_argument("--topic", action="append", help="discovery topic (repeatable; default: agenda+frontier)")
    p.add_argument("--per-topic", type=int, default=2, help="sources per topic per scout (default 2)")
    p.add_argument("--limit", type=int, default=3, help="max discovered sources to ingest (default 3)")
    p.add_argument("--arxiv-id", help="seed/acquire by arXiv id, e.g. 1706.03762")
    p.add_argument("--url", help="seed by URL (good for hitting the web_unknown tie-breaker)")
    p.add_argument("--query", help="acquire by free-text query")
    p.add_argument("--requester", choices=["pi", "researcher", "novelty"], default="researcher")
    p.add_argument("--no-llm", action="store_true", help="deterministic only — don't build the router/curator")
    return p.parse_args(argv)


def main() -> int:
    try:
        return asyncio.run(run(_parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
