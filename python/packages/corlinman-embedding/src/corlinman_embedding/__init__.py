"""corlinman-embedding — local or remote embedding dispatch.

Responsibility: present a single ``embed(texts) -> list[list[float]]`` API
to the agent loop; route to a local sentence-transformers pool or to a
remote provider per config. See plan §5.2 RAG data flow.

Also exposes the :mod:`corlinman_embedding.rerank_client` backends
(local cross-encoder / remote HTTP) used by the optional RAG rerank
stage (Sprint 3 T6).

The router, local pool, and remote HTTP client are importable without the
``[local]`` extra; local model loading stays lazy.
"""

from __future__ import annotations

from corlinman_embedding.benchmark import BenchmarkReport, benchmark_embedding
from corlinman_embedding.local_pool import LocalEmbeddingPool
from corlinman_embedding.provider import (
    CorlinmanEmbeddingProvider,
    GoogleEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from corlinman_embedding.remote_client import RemoteEmbeddingClient
from corlinman_embedding.rerank_client import (
    LocalRerankProvider,
    RemoteRerankProvider,
    RerankHit,
    RerankProvider,
)
from corlinman_embedding.router import EmbeddingBackend, EmbeddingConfig, EmbeddingRouter

__all__: list[str] = [
    "BenchmarkReport",
    "CorlinmanEmbeddingProvider",
    "EmbeddingBackend",
    "EmbeddingConfig",
    "EmbeddingRouter",
    "GoogleEmbeddingProvider",
    "LocalEmbeddingPool",
    "LocalRerankProvider",
    "OpenAICompatibleEmbeddingProvider",
    "RemoteEmbeddingClient",
    "RemoteRerankProvider",
    "RerankHit",
    "RerankProvider",
    "benchmark_embedding",
]
