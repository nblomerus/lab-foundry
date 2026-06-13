"""
Experiment LLM broker — the sandbox's ONLY window to a model.

The experiment sandbox is deliberately `--network none`: untrusted, LLM-generated code
must never reach the DB, the API, Zep/Neo4j, or the internet. That isolation made every
model-behaviour hypothesis untestable (the lab's designers were reduced to SIMULATING
outcomes — fabricated evidence). This broker re-admits exactly one capability, inference,
without widening anything else:

  * It listens on a UNIX SOCKET (no TCP, nothing routable) that the Quartermaster
    bind-mounts into each container at /sock/ollama.sock alongside a tiny stdlib helper
    (agents/experiments/sandbox_llm.py → /opt/lab/llm.py).
  * It forwards ONLY whitelisted inference endpoints to the host Ollama — generate /
    chat / embed / tags. No /api/pull (disk abuse), no /api/delete, no /api/create.
  * Responses are forced non-streaming (`stream: false`), bodies are size-capped, and
    every upstream call holds the SAME GPULock the agents' Router uses, so experiments
    and agents never fight for the GPU.

HTTP parsing is intentionally minimal: the only client is our mounted helper, which
always sends Content-Length'd JSON. Anything malformed gets a 400 and a closed
connection — there is no promise of general HTTP service here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

SOCKET_PATH = os.environ.get("EXPERIMENT_LLM_SOCKET", "/tmp/labfoundry-llm-broker.sock")
UPSTREAM = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CALL_TIMEOUT_S = float(os.environ.get("EXPERIMENT_LLM_TIMEOUT_S", "300"))
MAX_BODY_BYTES = int(os.environ.get("EXPERIMENT_LLM_MAX_BODY", str(2 * 1024 * 1024)))

# path -> method. The whole admitted surface; everything else is 404.
_ALLOWED = {
    "/api/generate": "POST",
    "/api/chat": "POST",
    "/api/embed": "POST",
    "/api/embeddings": "POST",
    "/api/tags": "GET",
}


def _http_response(status: int, body: bytes, reason: str = "") -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason or ('OK' if status == 200 else 'Error')}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


def _error(status: int, message: str) -> bytes:
    return _http_response(status, json.dumps({"error": message}).encode())


class LLMBroker:
    """One asyncio UDS server per harness; share the Router's GPULock at construction."""

    def __init__(self, *, gpu_lock, socket_path: str = SOCKET_PATH, upstream: str = UPSTREAM):
        self._gpu_lock = gpu_lock
        self._socket_path = socket_path
        self._upstream = upstream
        self._server: asyncio.AbstractServer | None = None
        self._client: httpx.AsyncClient | None = None
        self.requests_served = 0  # observability (lab_doctor / debugging)

    async def start(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)
        self._client = httpx.AsyncClient(base_url=self._upstream, timeout=CALL_TIMEOUT_S)
        self._server = await asyncio.start_unix_server(self._handle, path=self._socket_path)
        # The container runs as a non-root uid; the bind-mounted socket must be connectable.
        os.chmod(self._socket_path, 0o666)
        log.info("llm broker: listening on %s → %s (inference-only)", self._socket_path, self._upstream)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._client is not None:
            await self._client.aclose()
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._serve_one(reader)
        except Exception as e:  # noqa: BLE001 — a bad request must not kill the broker
            log.warning("llm broker: request failed: %s", e)
            response = _error(500, str(e)[:200])
        try:
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve_one(self, reader: asyncio.StreamReader) -> bytes:
        # Request line + headers (the helper always sends Content-Length'd JSON).
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.IncompleteReadError, TimeoutError):
            return _error(400, "malformed request")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, path, _version = lines[0].split(" ", 2)
        except ValueError:
            return _error(400, "malformed request line")
        path = path.split("?", 1)[0]
        if _ALLOWED.get(path) != method:
            return _error(404, f"not brokered: {method} {path} (inference endpoints only)")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length > MAX_BODY_BYTES:
            return _error(413, "body too large")
        body = await asyncio.wait_for(reader.readexactly(length), timeout=60) if length else b""

        if method == "GET":
            upstream = await self._client.get(path)
            self.requests_served += 1
            return _http_response(upstream.status_code, upstream.content)

        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return _error(400, "body is not JSON")
        if not isinstance(payload, dict):
            return _error(400, "body must be a JSON object")
        payload["stream"] = False  # one JSON response; the minimal parser doesn't stream
        model = str(payload.get("model") or "")
        if not model:
            return _error(400, "missing 'model'")

        # Same lock the agents' Router holds — experiments never fight agents for the GPU.
        async with self._gpu_lock.acquire(model):
            upstream = await self._client.post(path, json=payload)
        self.requests_served += 1
        return _http_response(upstream.status_code, upstream.content)


async def run_llm_broker(gpu_lock, stop: asyncio.Event) -> None:
    """main.py entrypoint: serve until shutdown. Crash-safe — a failed start is logged
    loudly (experiments then see connection errors → designs fail visibly, not silently)."""
    broker = LLMBroker(gpu_lock=gpu_lock)
    try:
        await broker.start()
    except Exception:  # noqa: BLE001
        log.exception("llm broker: FAILED to start — sandbox model access unavailable")
        return
    try:
        await stop.wait()
    finally:
        await broker.stop()
