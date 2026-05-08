"""Local embedding pool — async wrapper around sentence-transformers.

Responsibility: run sentence-transformers off the asyncio event loop. Model is
loaded lazily, and tests/callers may inject an encoder for deterministic
offline operation.

Requires the ``[local]`` extra (``sentence-transformers``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

import structlog

logger = structlog.get_logger(__name__)


class _Encoder(Protocol):
    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        ...


class LocalEmbeddingPool:
    """Local embedding backend with lazy sentence-transformers loading.

    ``encoder`` is intentionally injectable so CI and offline deployments can
    provide a tiny deterministic implementation without downloading a model.
    """

    def __init__(
        self,
        *,
        model_name: str,
        encoder: _Encoder | Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
    ) -> None:
        self.model_name = model_name
        self._encoder = encoder

    def _load(self) -> _Encoder | Callable[[Sequence[str]], Sequence[Sequence[float]]]:
        if self._encoder is not None:
            return self._encoder
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "LocalEmbeddingPool requires the `sentence-transformers` package. "
                "Install via `pip install corlinman-embedding[local]`."
            ) from exc
        logger.info("loading embedding model", model=self.model_name)
        self._encoder = SentenceTransformer(self.model_name)
        return self._encoder

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        _ = params
        if not texts:
            return []

        encoder = self._load()
        vectors = await asyncio.to_thread(self._encode_sync, encoder, list(texts))
        out = [[float(v) for v in vector] for vector in vectors]
        for idx, vector in enumerate(out):
            if len(vector) != dimension:
                raise ValueError(
                    f"embedding[{idx}] expected dimension {dimension}, got {len(vector)}"
                )
        return out

    @staticmethod
    def _encode_sync(
        encoder: _Encoder | Callable[[Sequence[str]], Sequence[Sequence[float]]],
        texts: list[str],
    ) -> Sequence[Sequence[float]]:
        if callable(encoder) and not hasattr(encoder, "encode"):
            return encoder(texts)
        result = encoder.encode(texts)  # type: ignore[union-attr]
        if hasattr(result, "tolist"):
            return cast(Sequence[Sequence[float]], result.tolist())
        return cast(Sequence[Sequence[float]], result)


__all__ = ["LocalEmbeddingPool"]
