"""Shared pytest fixtures.

`labfoundry_corpus.tools` keeps a lazily-created module-level asyncpg pool and
embedder (one event loop in production — the harness/API). pytest-asyncio gives
each async test its OWN event loop, so a pool created in one test's loop raises
``asyncpg.InterfaceError: another operation is in progress`` when a later test
reuses the cached singleton on a different loop. Reset the singletons around
every test so each builds its own on its current loop. No-op for tests that
never touch the corpus (they never create the pool).
"""

import pytest


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
