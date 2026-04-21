"""Embedding provider abstraction — Feature C §2.

``CorlinmanEmbeddingProvider`` is a parallel hierarchy to
:class:`corlinman_providers.CorlinmanProvider` for embedding-only wire
shapes (chat-completion providers sometimes have an embedding endpoint
too, but not always — e.g. Anthropic doesn't). The primary implementation
:class:`OpenAICompatibleEmbeddingProvider` handles OpenAI + every gateway
that speaks the OpenAI ``/v1/embeddings`` wire format (vLLM, Ollama,
SiliconFlow, local text-embeddings-inference, …).

A second implementation, :class:`GoogleEmbeddingProvider`, covers Gemini
embeddings via ``google-genai`` so the abstraction isn't a single-provider
fiction.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

import structlog
from corlinman_providers import EmbeddingSpec, ProviderKind

logger = structlog.get_logger(__name__)


class CorlinmanEmbeddingProvider(ABC):
    """Abstract base for every embedding-wire-shape adapter."""

    name: ClassVar[str]
    kind: ClassVar[ProviderKind]

    @classmethod
    @abstractmethod
    def params_schema(cls) -> dict[str, Any]:
        """JSON Schema (draft 2020-12) for per-request params."""

    @classmethod
    @abstractmethod
    def build(cls, spec: EmbeddingSpec, *, api_key: str | None, base_url: str | None) -> (
        CorlinmanEmbeddingProvider
    ):
        """Construct from an :class:`EmbeddingSpec` + resolved provider creds.

        The caller (gateway / registry wrapper) reads the
        ``[providers.<name>]`` referenced by ``spec.provider`` and passes
        its resolved ``api_key`` + ``base_url`` here so the embedding
        provider doesn't re-parse config.
        """

    @abstractmethod
    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        """Compute one embedding vector per input text.

        Implementations MUST assert that the returned vectors have length
        ``dimension`` (or document the deviation in the benchmark warnings).
        """


_OPENAI_EMBEDDING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "encoding_format": {
            "type": "string",
            "enum": ["float", "base64"],
            "description": "Embedding encoding; ``float`` for portability.",
        },
        "dimensions": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Requested embedding dimension (text-embedding-3* only). "
                "Ignored by models that don't support Matryoshka truncation."
            ),
        },
        "user": {
            "type": "string",
            "maxLength": 200,
            "description": "Opaque user identifier for abuse monitoring.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
    },
}


class OpenAICompatibleEmbeddingProvider(CorlinmanEmbeddingProvider):
    """OpenAI-wire-format embedding provider.

    Works with ``api.openai.com``, Azure OpenAI (via compatible base_url),
    vLLM, Ollama, SiliconFlow, text-embeddings-inference, any other
    gateway that exposes ``POST /v1/embeddings``.
    """

    name: ClassVar[str] = "openai_compatible"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPATIBLE

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str | None,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY") or None
        self._base_url = base_url

    @classmethod
    def build(
        cls,
        spec: EmbeddingSpec,
        *,
        api_key: str | None,
        base_url: str | None,
    ) -> OpenAICompatibleEmbeddingProvider:
        return cls(model=spec.model, api_key=api_key, base_url=base_url)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return _OPENAI_EMBEDDING_SCHEMA

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        if not self._api_key:
            raise RuntimeError("API key missing for openai_compatible embedding provider")

        from openai import AsyncOpenAI  # type: ignore[import-not-found]

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        client = AsyncOpenAI(**client_kwargs)

        call_kwargs: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
        }
        if params:
            # Caller is responsible for validating params against
            # ``params_schema()``; we forward as-is so unknown-but-accepted
            # keys (``user`` / ``encoding_format``) still work.
            for k, v in params.items():
                if k == "timeout_ms":
                    continue  # handled by the client, not the request body
                call_kwargs.setdefault(k, v)
        # Default to "dimensions" from config if not provided in params.
        call_kwargs.setdefault("dimensions", dimension)

        resp = await client.embeddings.create(**call_kwargs)
        # Response shape: {"data": [{"embedding": [...]}, ...]}
        return [list(item.embedding) for item in resp.data]


_GOOGLE_EMBEDDING_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {
            "type": "string",
            "enum": [
                "RETRIEVAL_QUERY",
                "RETRIEVAL_DOCUMENT",
                "SEMANTIC_SIMILARITY",
                "CLASSIFICATION",
                "CLUSTERING",
            ],
            "description": "Gemini task-type hint; tunes the embedding head.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
    },
}


class GoogleEmbeddingProvider(CorlinmanEmbeddingProvider):
    """Gemini embedding adapter via ``google-genai``.

    Included so the :class:`CorlinmanEmbeddingProvider` abstraction has more
    than one implementation — OpenAI-compatible stays the primary target.
    """

    name: ClassVar[str] = "google"
    kind: ClassVar[ProviderKind] = ProviderKind.GOOGLE

    def __init__(self, *, model: str, api_key: str | None) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY") or None

    @classmethod
    def build(
        cls,
        spec: EmbeddingSpec,
        *,
        api_key: str | None,
        base_url: str | None,
    ) -> GoogleEmbeddingProvider:
        _ = base_url  # Google ignores base_url; accepted for signature parity.
        return cls(model=spec.model, api_key=api_key)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return _GOOGLE_EMBEDDING_SCHEMA

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        if not self._api_key:
            raise RuntimeError("API key missing for Google embedding provider")

        from google import genai  # type: ignore[import-not-found]

        client = genai.Client(api_key=self._api_key)
        config: dict[str, Any] = {}
        if params and "task_type" in params:
            config["task_type"] = params["task_type"]
        # google-genai exposes embed_content at aio.models.embed_content;
        # wire the dimension when the model supports it (gemini-embedding-*).
        config["output_dimensionality"] = dimension

        resp = await client.aio.models.embed_content(
            model=self._model,
            contents=list(texts),
            config=config or None,  # type: ignore[arg-type]
        )
        # Response: resp.embeddings is a list; each has .values. google-genai
        # stubs declare both ``embeddings`` and ``.values`` as Optional; we
        # assert them non-None and fall back to an empty list otherwise.
        out: list[list[float]] = []
        for e in resp.embeddings or []:
            vals = getattr(e, "values", None) or []
            out.append(list(vals))
        return out


__all__ = [
    "CorlinmanEmbeddingProvider",
    "GoogleEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
]
