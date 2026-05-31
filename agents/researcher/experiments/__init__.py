"""
Experiment dispatcher: the "do something" layer of the researcher loop.

Each experiment kind is an async runner registered in `REGISTRY`. The loop's
`plan_inquiry` step proposes which kind to run with which params; the
orchestrator calls `dispatch(kind, params, dispatcher=...)` and the runner
returns a JSON-serialisable result.

Runners receive the full `dispatcher` so they can reach `state`, `router`,
and `curator` — useful for kinds like `fetch_pricing` that fetch a page and
then ask the model to parse it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class UnknownExperiment(Exception):
    """Raised when no implementation is registered for an experiment kind."""


# kind -> async runner: `(params: dict, *, dispatcher) -> dict`
REGISTRY: dict[str, Callable[..., Awaitable[dict]]] = {}


async def dispatch(kind: str, params: dict, *, dispatcher) -> dict:
    """
    Run the experiment named `kind` with `params`. Returns the result dict
    (JSON-serialisable). Raises `UnknownExperiment` if the kind isn't
    registered; the loop catches that and persists a failed experiment row.
    """
    runner = REGISTRY.get(kind)
    if runner is None:
        raise UnknownExperiment(f"no runner registered for kind={kind!r}")
    return await runner(params, dispatcher=dispatcher)


# Import each kind module at package load so they register themselves.
# Done at the bottom to avoid circular-import surprises.
from agents.researcher.experiments import (  # noqa: E402, F401  (side-effect import)
    compare_repo_growth,
    count_demand_signal,
    fetch_pricing,
    gh_search_trend,
)
