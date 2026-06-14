"""Lab-provided LLM access INSIDE the offline experiment sandbox.

This file is bind-mounted read-only into every experiment container at /opt/lab/llm.py.
The sandbox has NO network; the only model access is a unix socket (/sock/ollama.sock)
served by the harness's inference-only broker (harness/llm_broker.py). Stdlib only —
the experiment image has no requests/httpx.

Usage from generated experiment code:

    import sys
    sys.path.insert(0, "/opt/lab")
    import llm

    text = llm.generate("mistral:7b-instruct-q4_K_M", "What is 17*23?", temperature=0.0)
    reply = llm.chat("qwen2.5:14b-instruct-q4_K_M", [{"role": "user", "content": "hi"}])
    vecs = llm.embed("nomic-embed-text", ["a sentence", "another"])
    lp = llm.chat_logprobs("mistral:7b-instruct-q4_K_M", msgs, top_logprobs=5)  # per-token logprobs
    names = llm.models()

Calls are serialized with the lab's other GPU work and take seconds (7B) to a minute+
(27B+); budget the experiment's wall clock accordingly. Raises RuntimeError on broker
errors — let it fail loudly; never fabricate a model response.
"""

import http.client
import json
import os
import socket

SOCKET = os.environ.get("LLM_SOCKET", "/sock/ollama.sock")
DEFAULT_TIMEOUT_S = 600


class _UDSConnection(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(SOCKET)
        self.sock = s


def _request(method, path, payload=None, timeout=DEFAULT_TIMEOUT_S):
    conn = _UDSConnection(timeout)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"llm broker {resp.status}: {data[:300].decode('utf-8', 'replace')}")
        return json.loads(data)
    finally:
        conn.close()


def generate(model, prompt, system=None, timeout=DEFAULT_TIMEOUT_S, **options):
    """One completion. `options` are Ollama options (temperature, top_p, seed, num_predict...)."""
    payload = {"model": model, "prompt": prompt, "options": options}
    if system:
        payload["system"] = system
    return _request("POST", "/api/generate", payload, timeout)["response"]


def chat(model, messages, timeout=DEFAULT_TIMEOUT_S, **options):
    """One chat turn over [{'role','content'},...]; returns the assistant text."""
    out = _request("POST", "/api/chat", {"model": model, "messages": messages, "options": options}, timeout)
    return out["message"]["content"]


def embed(model, inputs, timeout=DEFAULT_TIMEOUT_S):
    """Embeddings for a string or list of strings; returns list[list[float]]."""
    out = _request("POST", "/api/embed", {"model": model, "input": inputs}, timeout)
    return out["embeddings"]


def chat_logprobs(model, messages, top_logprobs=5, timeout=DEFAULT_TIMEOUT_S, **options):
    """One chat turn WITH per-token logprobs (OpenAI-compatible endpoint — the local models DO expose
    these). Returns choices[0].logprobs.content: a list of
        {"token": str, "logprob": float, "top_logprobs": [{"token": str, "logprob": float}, ...]}
    Use it for calibration / ECE / perplexity / confidence work. `options` are OpenAI params at the top
    level (temperature, max_tokens, seed, ...). The plain text reply is choices[0].message.content."""
    payload = {"model": model, "messages": messages, "logprobs": True, "top_logprobs": top_logprobs, **options}
    out = _request("POST", "/v1/chat/completions", payload, timeout)
    return out["choices"][0]["logprobs"]["content"]


def models():
    """Names of the locally available models."""
    return [m["name"] for m in _request("GET", "/api/tags")["models"]]
