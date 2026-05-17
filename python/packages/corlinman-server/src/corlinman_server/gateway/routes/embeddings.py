"""``POST /v1/embeddings`` — OpenAI-compatible embedding endpoint.

Python port of ``rust/crates/corlinman-gateway/src/routes/embeddings.rs``.
The Rust file ships a 501 stub; the Python port goes a small step
further by wiring the in-process
:class:`corlinman_embedding.Embedder` Protocol when supplied. With
no embedder the route mirrors the Rust ``not_implemented`` envelope.

Request shape (OpenAI-compatible)::

    {
      "model": "text-embedding-3-small",
      "input": "hello world"  | ["hello", "world"]
    }

Response shape (OpenAI-compatible)::

    {
      "object": "list",
      "model": "...",
      "data": [
        {"object": "embedding", "index": 0, "embedding": [...]},
        ...
      ],
      "usage": {"prompt_tokens": 0, "total_tokens": 0}
    }
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EmbeddingRequest",
    "EmbedderFn",
    "router",
]


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible request body. Only the fields the in-process
    embedder consumes are validated; everything else is forwarded
    verbatim if a future embedder cares.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str] = Field(default_factory=list)


#: Embedder hook the route delegates to. ``input`` is always normalised
#: to a list of strings before the call so adapters don't need to
#: special-case the single-string OpenAI form.
EmbedderFn = Callable[[str, list[str]], Awaitable[Sequence[Sequence[float]]]]


def router(embedder: EmbedderFn | None = None) -> APIRouter:
    """Build the ``/v1/embeddings`` sub-router.

    :param embedder: async callable ``(model, [input1, ...]) -> [[v1, ...], ...]``.
        ``None`` returns the Rust-equivalent 501 envelope. Production
        boot wires this against
        :func:`corlinman_embedding.Embedder.embed`.
    """
    api = APIRouter()

    @api.post("/v1/embeddings")
    async def create_embeddings(req: EmbeddingRequest) -> JSONResponse:  # noqa: D401
        """Run the configured embedder against the supplied input."""
        if embedder is None:
            return JSONResponse(
                {
                    "error": "not_implemented",
                    "route": "/v1/embeddings",
                    "message": (
                        "no Embedder wired; build router(embedder=...)"
                    ),
                },
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
            )
        inputs: list[str] = (
            [req.input] if isinstance(req.input, str) else list(req.input)
        )
        try:
            vectors = await embedder(req.model, inputs)
        except Exception as exc:  # noqa: BLE001 — return a typed error envelope
            return JSONResponse(
                {
                    "error": {
                        "code": "embedder_error",
                        "message": str(exc),
                    },
                },
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        data: list[dict[str, Any]] = [
            {
                "object": "embedding",
                "index": idx,
                "embedding": list(vec),
            }
            for idx, vec in enumerate(vectors)
        ]
        return JSONResponse(
            {
                "object": "list",
                "model": req.model,
                "data": data,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
        )

    return api
