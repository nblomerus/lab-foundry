"""Shared pytest fixtures.

`labfoundry_corpus.tools` keeps a lazily-created module-level asyncpg pool and
embedder (one event loop in production — the harness/API). pytest-asyncio gives
each async test its OWN event loop, so a pool created in one test's loop raises
``asyncpg.InterfaceError: another operation is in progress`` when a later test
reuses the cached singleton on a different loop. Reset the singletons around
every test so each builds its own on its current loop. No-op for tests that
never touch the corpus (they never create the pool).
"""

import os

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    """A `PostgresClient` on a clean DATABASE_URL pool.

    Skips when no migrated DB is reachable (mirrors the corpus/ingest tests).
    Truncates the core lab tables with RESTART IDENTITY so each test starts from
    a known-empty, predictable-id state, and seeds the single `company_state` row.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set — DB-backed test needs a migrated DB")

    import asyncpg

    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"DB unreachable: {e}")

    from state.client import PostgresClient

    try:
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT to_regclass('public.claims')") is None:
                pytest.skip("schema not applied (no claims table)")
            # SAFETY GUARD: the TRUNCATE below is `... CASCADE`, and `documents` has a
            # FK to `agent_runs`, so truncating agent_runs CASCADEs into documents+chunks
            # — i.e. it would DELETE THE WHOLE CORPUS. This fixture is for a *disposable*
            # test DB, never the live one. A populated corpus means DATABASE_URL points at
            # production; refuse rather than wipe it. (Set LABFOUNDRY_ALLOW_DB_WIPE=1 to
            # override for an intentional throwaway DB.)
            if os.environ.get("LABFOUNDRY_ALLOW_DB_WIPE") != "1":
                doc_count = await conn.fetchval("SELECT count(*) FROM documents") or 0
                if doc_count > 100:
                    pytest.skip(
                        f"REFUSING to truncate: DATABASE_URL points at a populated corpus "
                        f"({doc_count} documents) — looks like production. Use a disposable "
                        f"test DB, or set LABFOUNDRY_ALLOW_DB_WIPE=1 if you really mean it."
                    )
            await conn.execute(
                "TRUNCATE claims, events, tasks, findings, critic_verdicts, "
                "research_inquiries, evidence, experiment_runs, fetch_cache, agent_runs "
                "RESTART IDENTITY CASCADE"
            )
            await conn.execute(
                "INSERT INTO company_state (id, problem_statement, deadline) "
                "VALUES (1, 'test problem', now() + interval '30 days') "
                "ON CONFLICT (id) DO NOTHING"
            )
        yield PostgresClient(pool=pool)
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
def _reset_corpus_singletons():
    try:
        from library.corpus import tools
    except Exception:
        # pgvector/asyncpg not importable in this environment — nothing to reset.
        yield
        return

    tools._pool = None
    tools._embedder = None
    yield
    tools._pool = None
    tools._embedder = None
