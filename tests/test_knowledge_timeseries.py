"""Pure-logic tests for GET /knowledge/timeseries.

Input validation (metric/bucket/kind allowlists) short-circuits BEFORE any DB
access, so these run with no database. The dummy request's pool access raises,
asserting that an invalid request never reaches the corpus. The SQL aggregation
itself is verified by live curl against the running API, not here — we don't
pytest against the live corpus.
"""

import pytest

from api.knowledge import _DOC_JOIN_METRICS, _METRIC_EVENT, knowledge_timeseries


class _State:
    @property
    def pool(self):  # pragma: no cover - reaching this is the failure we assert against
        raise AssertionError("validation must return before any DB access")


class _App:
    state = _State()


class _Req:
    """Stand-in for FastAPI's Request; touching .app.state.pool raises."""

    app = _App()


@pytest.mark.asyncio
async def test_unknown_metric_returns_error():
    res = await knowledge_timeseries(_Req(), metric="bogus")
    assert res["status"] == "error"
    assert res["points"] == []


@pytest.mark.asyncio
async def test_unknown_bucket_returns_error():
    res = await knowledge_timeseries(_Req(), metric="ingested", bucket="week")
    assert res["status"] == "error"
    assert res["points"] == []


@pytest.mark.asyncio
async def test_unknown_kind_returns_error():
    res = await knowledge_timeseries(_Req(), metric="ingested", kind="pinterest")
    assert res["status"] == "error"


def test_metric_event_map_is_complete():
    assert set(_METRIC_EVENT) == {"discovered", "parsed", "ingested", "certified", "quarantined"}
    # certified is an alias of the ingest event
    assert _METRIC_EVENT["certified"] == _METRIC_EVENT["ingested"] == "document.ingested"
    # doc-join metrics are a subset; 'discovered' scopes via payload, not a join
    assert set(_METRIC_EVENT) >= _DOC_JOIN_METRICS
    assert "discovered" not in _DOC_JOIN_METRICS
