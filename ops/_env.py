"""Shared ops helpers — .env loading + pgvector codec registration.

A leaf module with no side effects, so the ops scripts can import these at module
top (imports belong at the top, never inside a function)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pgvector.asyncpg


def load_dotenv() -> None:
    """Best-effort: set vars from ./.env that aren't already in the environment.
    (python-dotenv is optional; the Makefile injects .env for `make`, but a bare
    `python -m ops.<tool>` run needs this.)"""
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


async def register_vector_codec(conn) -> None:
    """Register the pgvector codec on an asyncpg connection so list[float] binds
    as vector(768) (corpus embed writes need it). Mirrors harness/main.py."""
    try:
        await pgvector.asyncpg.register_vector(conn)
    except Exception as e:  # noqa: BLE001 — without it, embed writes fail, not stage
        print(f"  pgvector codec not registered (embed writes will fail): {e}", file=sys.stderr)
