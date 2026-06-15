"""Every registered curator recipe must have a router ROUTE entry.

A recipe whose invocation_type is absent from ROUTE fails LIVE at `router.invoke` with
"No route for invocation_type=..." — but passes any unit test that MOCKS the router. That is exactly
how the verification-spine recipe `evaluation.audit_finding` shipped broken: the deterministic proof
mocked router.invoke, so it went green, and the gap only surfaced on the live lab. This guard imports
every agents.*.handler so all lazily-registered recipes exist, then requires each to route.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil

import agents
from harness.curator import RECIPES
from harness.dispatch import CLOSURE_EXEMPT_EVENTS
from harness.router import ROUTE


def test_every_registered_recipe_has_a_route():
    for m in pkgutil.walk_packages(agents.__path__, "agents."):
        if m.name.endswith(".handler"):
            # a handler that can't import registers no recipes — skip it, don't fail the guard
            with contextlib.suppress(Exception):
                importlib.import_module(m.name)
    unrouted = sorted(k for k in RECIPES if k not in ROUTE)
    assert unrouted == [], f"recipes with no ROUTE entry (will fail live at router.invoke): {unrouted}"


def test_data_requested_is_closure_exempt():
    """`data.requested` is a deliberately-unhandled demand record (agents/ariadne/persist.request_data).
    It MUST stay in CLOSURE_EXEMPT_EVENTS, else the dispatcher's loop.unclosed guard false-alarms on
    every Ariadne dataset request — the emitted-but-unhandled class caught in pre-PR review."""
    assert "data.requested" in CLOSURE_EXEMPT_EVENTS
