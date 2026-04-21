"""Tests for ``benchmark_embedding`` + the OpenAI-compatible provider.

Strategy: stub out :class:`CorlinmanEmbeddingProvider` with a trivial
in-memory implementation so we exercise the benchmark math (latency
percentiles, cosine matrix, dimension cross-check, warnings) without a
live network call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from corlinman_embedding import (
    BenchmarkReport,
    CorlinmanEmbeddingProvider,
    benchmark_embedding,
)
from corlinman_providers import ProviderKind


class _FixedVectorProvider(CorlinmanEmbeddingProvider):
    """Returns a pre-recorded vector per input index."""

    name: ClassVar[str] = "fixed"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPATIBLE

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self._call_idx = 0

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": True}

    @classmethod
    def build(
        cls,
        spec: Any,
        *,
        api_key: str | None,
        base_url: str | None,
    ) -> _FixedVectorProvider:
        raise NotImplementedError  # test-only provider, never built from spec

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        _ = dimension, params
        idx = self._call_idx
        self._call_idx += 1
        return [self._vectors[idx]] * len(texts)


@pytest.mark.asyncio
async def test_benchmark_returns_expected_shape() -> None:
    """Happy path: 3 samples, 3-dim vectors → 3x3 matrix, diagonal = 1.0."""
    vectors = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [1.0, 1.0, 0.0],
    ]
    provider = _FixedVectorProvider(vectors)
    report = await benchmark_embedding(
        provider,
        ["a", "b", "c"],
        dimension=3,
    )

    assert isinstance(report, BenchmarkReport)
    assert report.dimension == 3
    assert len(report.similarity_matrix) == 3
    assert len(report.similarity_matrix[0]) == 3
    # Diagonal is always 1.0 (vector with itself).
    for i in range(3):
        assert report.similarity_matrix[i][i] == pytest.approx(1.0)
    # Orthogonal pair (0, 1) → 0.0
    assert report.similarity_matrix[0][1] == pytest.approx(0.0)
    # (0, 2) = [1,0,0]·[1,1,0] / (1 · sqrt(2)) = 1 / sqrt(2) ≈ 0.7071
    assert report.similarity_matrix[0][2] == pytest.approx(0.7071, rel=1e-3)
    assert report.latency_ms_p50 >= 0.0
    assert report.latency_ms_p99 >= report.latency_ms_p50
    assert report.warnings == []


@pytest.mark.asyncio
async def test_benchmark_warns_on_dimension_mismatch() -> None:
    """Config says dim=10 but provider returns 3-dim vectors → warning."""
    vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    provider = _FixedVectorProvider(vectors)
    report = await benchmark_embedding(provider, ["a", "b"], dimension=10)

    assert report.dimension == 3  # actual, not configured
    assert any("dimension mismatch" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_benchmark_empty_samples_rejected() -> None:
    provider = _FixedVectorProvider([])
    with pytest.raises(ValueError, match="at least one sample"):
        await benchmark_embedding(provider, [], dimension=8)


@pytest.mark.asyncio
async def test_benchmark_handles_empty_vectors_gracefully() -> None:
    """Empty vectors → zero row/col, warning emitted, matrix shape preserved."""
    vectors = [[1.0, 0.0], []]
    provider = _FixedVectorProvider(vectors)
    report = await benchmark_embedding(provider, ["a", "b"], dimension=2)

    assert len(report.similarity_matrix) == 2
    # Row 1 is all zeros (empty vec).
    assert report.similarity_matrix[1] == [0.0, 0.0]
    assert any("empty vector" in w for w in report.warnings)
