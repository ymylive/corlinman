"""Remote embedding client — HTTP client for hosted embedding APIs.

Responsibility: talk to OpenAI / vendor embedding endpoints via ``httpx``
and normalise responses to ``list[list[float]]``. Lives in the slim
``corlinman:1.0.0`` image (no torch dependency).

Implements the OpenAI-compatible ``POST /embeddings`` wire shape used by
OpenAI, vLLM, Ollama, SiliconFlow, and text-embeddings-inference. Retry and
project-wide error mapping remain higher-level integration work.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class RemoteEmbeddingClient:
    """OpenAI-compatible HTTP embedding client."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._timeout = timeout
        self._owned_client = client is None
        self._client: httpx.AsyncClient | None = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owned_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the owned HTTP client, if any. Idempotent."""
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimension: int,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        payload: dict[str, Any] = {
            "model": self.model,
            "input": list(texts),
            "dimensions": dimension,
            "encoding_format": "float",
        }
        if params:
            for key, value in params.items():
                if key == "timeout_ms":
                    continue
                payload[key] = value

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = await self._get_client()
        resp = await client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])

        indexed: list[tuple[int, list[float]]] = []
        unindexed: list[list[float]] = []
        for item in data:
            embedding = [float(v) for v in item.get("embedding", [])]
            index = item.get("index")
            if isinstance(index, int):
                indexed.append((index, embedding))
            else:
                unindexed.append(embedding)

        if indexed:
            indexed.sort(key=lambda row: row[0])
            return [embedding for _, embedding in indexed]
        return unindexed


__all__ = ["RemoteEmbeddingClient"]
