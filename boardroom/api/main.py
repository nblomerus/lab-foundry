"""
FastAPI command-center backend.

Endpoints:
    GET  /snapshot          — everything the dashboard needs on load
    GET  /events            — recent raw events
    GET  /theses/{id}/findings
    WS   /ws/events         — live event stream (push)

Run with:
    uvicorn boardroom.api.main:app --host 0.0.0.0 --port 8503 --reload
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boardroom.api import snapshot, stream


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
        db_url, min_size=2, max_size=20, init=_init_conn,
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


app = FastAPI(title="Boardroom Command Center", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:3001", "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(snapshot.router)
app.include_router(stream.router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
