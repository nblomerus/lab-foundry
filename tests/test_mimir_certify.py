"""Mimir's LLM trust tie-breaker — MimirVerdict + _certify_llm. Pure: a fake
curator/router stand in for the model, so no DB and no real LLM call."""

import pydantic
import pytest

from agents.mimir.handler import MimirVerdict, _certify_llm

pytestmark = pytest.mark.asyncio


class _FakeCurator:
    async def build(self, invocation_type, context):
        assert invocation_type == "mimir.certify"
        self.context = context
        return {"prompt": "built"}


class _FakeRouter:
    def __init__(self, verdict):
        self._verdict = verdict

    async def invoke(self, *, prompt, output_schema_class, session=None, step_name=None):
        assert output_schema_class is MimirVerdict
        return self._verdict, 123


async def test_certify_llm_returns_verdict():
    verdict = MimirVerdict(decision="approve", tier="web_reputable", reasons="reputable industry source with citations")
    out = await _certify_llm(
        {"title": "X", "source_url": "https://blog.example/p"}, _FakeCurator(), _FakeRouter(verdict), None
    )
    assert out is not None
    assert out.decision == "approve"
    assert out.tier == "web_reputable"


async def test_certify_llm_swallows_errors():
    class _Boom:
        async def build(self, *a, **k):
            raise RuntimeError("model down")

    out = await _certify_llm({"source_url": "https://x"}, _Boom(), None, None)
    assert out is None  # best-effort -> deterministic floor


async def test_mimir_verdict_cannot_mint_top_tier():
    # The LLM schema only admits the bottom three tiers; paper-grade trust needs
    # a verifiable identifier, settled deterministically.
    with pytest.raises(pydantic.ValidationError):
        MimirVerdict(decision="approve", tier="peer_reviewed", reasons="x" * 30)
