"""Embedding router — pick local pool or remote client per config.

Responsibility: read ``EmbeddingConfig`` (source = ``"local" | "remote"``,
model name, dim assertion) and route ``embed(texts)`` accordingly. Emits
metrics ``corlinman_embedding_batch_size`` (plan §9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import structlog

from corlinman_embedding.local_pool import LocalEmbeddingPool
from corlinman_embedding.remote_client import RemoteEmbeddingClient

logger = structlog.get_logger(__name__)


class EmbeddingBackend(Protocol):
    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class EmbeddingConfig:
    """Config needed to select an embedding backend."""

    source: Literal["local", "remote"]
    model: str
    dimension: int = 3072
    base_url: str | None = None
    api_key: str | None = None


class EmbeddingRouter:
    """Routes embedding requests to a local or remote backend."""

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        local_pool: EmbeddingBackend | None = None,
        remote_client: EmbeddingBackend | None = None,
    ) -> None:
        self.config = config
        self._local_pool = local_pool
        self._remote_client = remote_client

    async def embed(
        self,
        texts: Sequence[str],
        *,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        backend = self._backend()
        batch = list(texts)
        logger.info(
            "embedding batch",
            source=self.config.source,
            model=self.config.model,
            batch_size=len(batch),
        )
        vectors = await backend.embed(batch, dimension=self.config.dimension, params=params)
        self._assert_shape(vectors, expected_count=len(batch))
        return vectors

    def _backend(self) -> EmbeddingBackend:
        if self.config.source == "local":
            if self._local_pool is None:
                self._local_pool = LocalEmbeddingPool(model_name=self.config.model)
            return self._local_pool

        if self._remote_client is None:
            if not self.config.base_url:
                raise ValueError("remote embedding requires base_url")
            self._remote_client = RemoteEmbeddingClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                model=self.config.model,
            )
        return self._remote_client

    def _assert_shape(self, vectors: list[list[float]], *, expected_count: int) -> None:
        if len(vectors) != expected_count:
            raise ValueError(f"expected {expected_count} embeddings, got {len(vectors)}")
        for idx, vector in enumerate(vectors):
            if len(vector) != self.config.dimension:
                raise ValueError(
                    f"embedding[{idx}] expected dimension {self.config.dimension}, "
                    f"got {len(vector)}"
                )


__all__ = ["EmbeddingBackend", "EmbeddingConfig", "EmbeddingRouter"]
