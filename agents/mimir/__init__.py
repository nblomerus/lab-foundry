"""Mimir, Warden of the Library (MIMIR_WARDEN_SCOPE §4).

The single agent that owns corpus ingest AND trust: on a discovered source it
orchestrates the deterministic ingest tools (library.ingest.pipeline) with
classify_trust (library.trust) as the gate between staging and embedding — no
separate Librarian agent, no inter-agent event handshake. See handler.py.
"""
