"""
LabFoundry command-center backend (autonomous research lab).

Endpoints:
    GET  /snapshot          — everything the dashboard needs on load
    GET  /events            — recent raw events
    GET  /claims/{id}/findings
    WS   /ws/events         — live event stream (push)

Run with:
    uvicorn api.main:app --host 0.0.0.0 --port 8503 --reload
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

# Load .env BEFORE any labfoundry imports so DEEPSEEK_API_KEY etc. are present
# when the router builds its provider chains at import time. systemd already
# loads .env via EnvironmentFile=; this is for manual `uvicorn api.main:app`
# launches (e.g. the demo on a side instance) where forgetting to `set -a` and
# source .env silently disables paid providers and the cost panel.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is a runtime dep; skip if not installed

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import bench, debug, knowledge, snapshot, stream, trace


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSONB ↔ dict codec so payload columns aren't returned as strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = os.environ["DATABASE_URL"]
    log.info("opening Postgres pool")
    app.state.pool = await asyncpg.create_pool(
        db_url,
        min_size=2,
        max_size=20,
        init=_init_conn,
    )

    hub = stream.StreamHub()
    await hub.start(app.state.pool)
    app.state.stream_hub = hub

    log.info("api ready")
    try:
        yield
    finally:
        log.info("shutting down")
        await hub.stop()
        await app.state.pool.close()


app = FastAPI(title="LabFoundry Command Center", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3030",
        "http://127.0.0.1:3030",
        "http://localhost:8088",
        "http://127.0.0.1:8088",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(snapshot.router)
app.include_router(stream.router)
app.include_router(bench.router)
app.include_router(debug.router)
app.include_router(trace.router)
app.include_router(knowledge.router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
