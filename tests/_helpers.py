"""Shared test helpers — run agent / handler / api / library tests with NO real Postgres,
Neo4j, Ollama, or network. Everything is mocked deterministically.

The building blocks:
  ScriptedPool / ScriptedConn  — a fake asyncpg pool+conn. Supports both `pool.fetch(...)` and
                                 `async with pool.acquire() as conn: conn.fetch(...)`. Configure
                                 with `rules=[(sql_substring, result), ...]` (first match wins).
  patch_chain(mp, *modules)    — patch the shared LLM `_chain_complete` in each module to canned
                                 output (str | list[str] sequential | callable). Returns the call log.
  FakeNeoDriver / fake_neo()   — a fake async Neo4j driver (session().run(...) → async records).
  FakeEmbedder                 — deterministic embeddings for the corpus/ingest paths.
  make_state(...)              — an AsyncMock state with a ScriptedPool + the common sync attrs.

Use AsyncMock for any state/dispatcher method your code awaits; set `.pool = ScriptedPool(...)`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

_MISS = object()


# ── scriptable asyncpg pool / conn ────────────────────────────────────────────
class _Ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_exc):
        return False


class ScriptedConn:
    """Fake asyncpg connection. `rules` = list of (sql_substring, result); first match wins.
    fetch→list, fetchrow→first row (or the result if a dict), fetchval→scalar (first col of first
    row, or the value), execute→status. Unmatched queries return the defaults."""

    def __init__(self, rules=None, *, default_fetch=None, default_val=None, default_exec="OK"):
        self.rules = list(rules or [])
        self._default_fetch = [] if default_fetch is None else default_fetch
        self._default_val = default_val
        self._default_exec = default_exec
        self.calls: list[tuple] = []

    def _match(self, sql):
        for sub, res in self.rules:
            if sub in sql:
                return res() if callable(res) else res
        return _MISS

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        r = self._match(sql)
        return self._default_fetch if r is _MISS else list(r)

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        r = self._match(sql)
        if r is _MISS:
            return None
        if isinstance(r, list):
            return r[0] if r else None  # empty-list rule → no row (don't IndexError)
        return r

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        r = self._match(sql)
        if r is _MISS:
            return self._default_val
        if isinstance(r, list):
            r = r[0] if r else None
        if isinstance(r, dict):
            return next(iter(r.values()), None)
        return r

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        r = self._match(sql)
        return self._default_exec if r is _MISS else r

    async def executemany(self, sql, args):
        self.calls.append(("executemany", sql, args))
        return self._default_exec

    def transaction(self):
        return _Ctx(None)


class ScriptedPool:
    """Fake asyncpg pool sharing ONE ScriptedConn (so `.calls` accumulates across acquire/direct)."""

    def __init__(self, rules=None, **kw):
        self.conn = ScriptedConn(rules, **kw)

    def acquire(self):
        return _Ctx(self.conn)

    async def fetch(self, *a):
        return await self.conn.fetch(*a)

    async def fetchrow(self, *a):
        return await self.conn.fetchrow(*a)

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)

    async def execute(self, *a):
        return await self.conn.execute(*a)

    async def executemany(self, *a):
        return await self.conn.executemany(*a)

    async def close(self):
        pass

    @property
    def calls(self):
        return self.conn.calls


# ── LLM chain patcher ─────────────────────────────────────────────────────────
def patch_chain(monkeypatch, *modules, content="{}"):
    """Patch `_chain_complete` in each module to canned output. `content` may be a str, a list of
    strings (returned in sequence), or a callable(messages, **kwargs) -> str. Returns the call log
    (list of (messages, kwargs)) so a test can assert what the model was asked."""
    seq = list(content) if isinstance(content, list) else None
    calls: list[tuple] = []

    async def _fake(messages, **kw):
        calls.append((messages, kw))
        if callable(content):
            return content(messages, **kw)
        if seq is not None:
            return seq.pop(0) if seq else "{}"
        return content

    for m in modules:
        monkeypatch.setattr(m, "_chain_complete", _fake, raising=False)
    return calls


# ── fake Neo4j ────────────────────────────────────────────────────────────────
class _NeoResult:
    def __init__(self, records):
        self._records = list(records)

    def __aiter__(self):
        self._it = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def single(self):
        return self._records[0] if self._records else None

    async def data(self):
        return list(self._records)


class _NeoSession:
    def __init__(self, on_run):
        self._on_run = on_run
        self.queries: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def run(self, query, **params):
        self.queries.append((query, params))
        recs = self._on_run(query, params) if callable(self._on_run) else self._on_run
        return _NeoResult(recs or [])


class FakeNeoDriver:
    """Fake async Neo4j driver. `on_run` is records (list of dict) or a callable(query, params)->records."""

    def __init__(self, on_run=None):
        self._on_run = on_run if on_run is not None else (lambda q, p: [])
        self.sessions: list[_NeoSession] = []

    def session(self, **_kw):
        s = _NeoSession(self._on_run)
        self.sessions.append(s)
        return s

    async def close(self):
        pass


def fake_neo(on_run=None):
    return FakeNeoDriver(on_run)


# ── fake embedder (corpus/ingest) ─────────────────────────────────────────────
class FakeEmbedder:
    """Deterministic embeddings — a fixed-dim vector seeded by text length, so cosine is stable."""

    def __init__(self, dim: int = 1024):
        self.dim = dim

    async def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            v[(len(t) or 1) % self.dim] = 1.0
            out.append(v)
        return out

    async def embed_one(self, text):
        return (await self.embed([text]))[0]


# ── state / dispatcher ─────────────────────────────────────────────────────────
def make_state(pool=None, *, sid: int = 1, triggered_by_event_id: int = 1, **returns):
    """An AsyncMock `state` with a ScriptedPool and the common sync attrs the handlers read.
    Pass `method=value` to preset an async method's return_value (e.g. get_company_state=cs)."""
    st = AsyncMock()
    st.pool = pool if pool is not None else ScriptedPool()
    st.id = sid
    st.triggered_by_event_id = triggered_by_event_id
    st.next_step_order = lambda: 1  # sync helper used by _record_run
    for name, val in returns.items():
        getattr(st, name).return_value = val
    return st


def make_dispatcher(state=None, *, router=None, curator=None, session=None, **attrs):
    """A simple dispatcher stub with .state/.router/.curator/.session + any extra attrs."""
    d = AsyncMock()
    d.state = state if state is not None else make_state()
    d.router = router
    d.curator = curator
    d.session = session
    for k, v in attrs.items():
        setattr(d, k, v)
    return d
