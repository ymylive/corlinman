"""Embedding benchmark helper — backs ``POST /admin/embedding/benchmark``.

Given an embedding provider and a list of sample strings, emits:

* p50 / p99 per-call latency (one call per sample — captures real-world
  single-string latency, which is what users care about for chat-sized
  queries; batch latency is out of scope for v1);
* the dimension actually returned by the provider (cross-checked against
  the configured ``dimension``);
* a cosine-similarity matrix across the samples (diagonal = 1.0);
* a list of warnings (dimension mismatch, empty vectors, etc.).
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from corlinman_embedding.provider import CorlinmanEmbeddingProvider

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class BenchmarkReport:
    """Output of :func:`benchmark_embedding`.

    Shape mirrors the ``BenchmarkView`` type in the contract's HTTP layer so
    the gateway can serialize it more or less verbatim.
    """

    dimension: int
    latency_ms_p50: float
    latency_ms_p99: float
    similarity_matrix: list[list[float]]
    warnings: list[str] = field(default_factory=list)


async def benchmark_embedding(
    provider: CorlinmanEmbeddingProvider,
    samples: Sequence[str],
    *,
    dimension: int,
    params: dict[str, Any] | None = None,
) -> BenchmarkReport:
    """Run one ``embed([sample])`` call per sample, gather stats.

    * ``dimension`` is the *configured* dimension; the report carries the
      *actual* dimension observed on the first non-empty response. A
      mismatch is surfaced as a warning — not an error — so ops can see
      the configuration drift without losing the benchmark output.
    * ``samples`` must be non-empty. Empty lists raise ``ValueError`` so
      the caller can't accidentally get an empty similarity matrix.
    """
    if not samples:
        raise ValueError("benchmark_embedding requires at least one sample")

    params = params or {}
    warnings: list[str] = []
    vectors: list[list[float]] = []
    latencies_ms: list[float] = []

    for idx, text in enumerate(samples):
        start = time.perf_counter()
        result = await provider.embed([text], dimension=dimension, params=params)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        latencies_ms.append(elapsed_ms)

        if not result:
            warnings.append(f"sample[{idx}]: provider returned no embeddings")
            vectors.append([])
            continue
        vec = result[0]
        if not vec:
            warnings.append(f"sample[{idx}]: provider returned empty vector")
            vectors.append([])
            continue
        vectors.append(vec)

    actual_dim = next((len(v) for v in vectors if v), 0)
    if actual_dim == 0:
        warnings.append("no usable embeddings returned; similarity matrix is empty")
    elif actual_dim != dimension:
        warnings.append(
            f"dimension mismatch: config={dimension} actual={actual_dim}"
        )

    similarity = _cosine_matrix(vectors)

    p50 = _percentile(latencies_ms, 0.50)
    p99 = _percentile(latencies_ms, 0.99)

    return BenchmarkReport(
        dimension=actual_dim,
        latency_ms_p50=p50,
        latency_ms_p99=p99,
        similarity_matrix=similarity,
        warnings=warnings,
    )


def _cosine_matrix(vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    """Return the NxN cosine-similarity matrix for ``vectors``.

    Empty vectors (missing responses) contribute an all-zero row/column so
    the matrix shape always matches ``len(vectors)``.
    """
    n = len(vectors)
    out: list[list[float]] = [[0.0] * n for _ in range(n)]
    norms = [math.sqrt(sum(v * v for v in vec)) for vec in vectors]
    for i in range(n):
        for j in range(i, n):
            if not vectors[i] or not vectors[j] or norms[i] == 0 or norms[j] == 0:
                sim = 0.0
            else:
                dot = sum(a * b for a, b in zip(vectors[i], vectors[j], strict=False))
                sim = dot / (norms[i] * norms[j])
            out[i][j] = sim
            out[j][i] = sim
    return out


def _percentile(values: list[float], q: float) -> float:
    """Simple percentile — linear interpolation between nearest ranks.

    We deliberately don't pull in numpy for this: latency lists are tiny
    (≤ 20 by contract) and numpy would dwarf every other import in this
    package.
    """
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


__all__ = ["BenchmarkReport", "benchmark_embedding"]
