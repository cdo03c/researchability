"""Tests for GeminiClient structured outputs, SQLite caching, and error handling."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from researchability.llm.client import (
    GeminiApiError,
    GeminiClient,
    GeminiConfig,
    MissingApiKeyError,
    ModelTier,
)


class SampleVerdict(BaseModel):
    passed: bool
    reason: str


def test_cache_hit_and_prompt_version_invalidation(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_transport(model_id: str, prompt: str, system: str, schema_cls, temp: float) -> str:
        calls.append((model_id, prompt))
        return '{"passed": true, "reason": "Verified from text."}'

    config = GeminiConfig(
        api_key="test-key",
        cache_path=tmp_path / "cache.sqlite",
    )
    client = GeminiClient(config=config, transport=fake_transport)

    res1, meta1 = client.generate_structured(
        paper_id="arxiv:2401.00001",
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt="Does this paper contain empirical experiments?",
        schema_cls=SampleVerdict,
        prompt_version="v1",
        tier=ModelTier.FAST,
    )
    assert res1.passed is True
    assert meta1.from_cache is False
    assert len(calls) == 1

    # Identical call must hit cache without calling transport
    res2, meta2 = client.generate_structured(
        paper_id="arxiv:2401.00001",
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt="Does this paper contain empirical experiments?",
        schema_cls=SampleVerdict,
        prompt_version="v1",
        tier=ModelTier.FAST,
    )
    assert res2 == res1
    assert meta2.from_cache is True
    assert len(calls) == 1

    # Bumping prompt_version or changing tier must miss cache
    _, meta3 = client.generate_structured(
        paper_id="arxiv:2401.00001",
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt="Does this paper contain empirical experiments?",
        schema_cls=SampleVerdict,
        prompt_version="v2",
        tier=ModelTier.REASONING,
    )
    assert meta3.from_cache is False
    assert meta3.model_id == config.reasoning_model
    assert len(calls) == 2


def test_invalid_schema_response_raises_gemini_api_error(tmp_path: Path) -> None:
    def bad_transport(model_id: str, prompt: str, system: str, schema_cls, temp: float) -> str:
        return '{"wrong_field": 123}'

    client = GeminiClient(
        config=GeminiConfig(api_key="test-key", cache_path=tmp_path / "cache.sqlite"),
        transport=bad_transport,
    )
    with pytest.raises(GeminiApiError, match="did not match schema SampleVerdict"):
        client.generate_structured(
            paper_id="arxiv:2401.00002",
            gate_id="G1.6_METHOD_COMPLETENESS",
            prompt="Check methods",
            schema_cls=SampleVerdict,
        )


def test_missing_api_key_raises_on_live_call_but_allows_cache_hit(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.sqlite"

    # Seed cache first with a mock transport
    seeded_client = GeminiClient(
        config=GeminiConfig(api_key="test-key", cache_path=cache_path),
        transport=lambda *_: '{"passed": false, "reason": "No experiments"}',
    )
    seeded_client.generate_structured(
        paper_id="arxiv:2401.00003",
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt="Check empirical claims",
        schema_cls=SampleVerdict,
    )

    # Offline client with no API key can read cached result
    offline_client = GeminiClient(
        config=GeminiConfig(api_key=None, cache_path=cache_path),
    )
    cached_res, meta = offline_client.generate_structured(
        paper_id="arxiv:2401.00003",
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt="Check empirical claims",
        schema_cls=SampleVerdict,
    )
    assert cached_res.passed is False
    assert meta.from_cache is True

    # Live cache miss without API key raises MissingApiKeyError
    with pytest.raises(MissingApiKeyError, match="GEMINI_API_KEY"):
        offline_client.generate_structured(
            paper_id="arxiv:2401.99999",
            gate_id="G0.1_EMPIRICAL_CLAIM",
            prompt="Uncached paper",
            schema_cls=SampleVerdict,
        )
