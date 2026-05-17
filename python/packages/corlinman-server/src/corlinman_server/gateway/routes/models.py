"""``GET /v1/models`` — OpenAI-compatible model discovery.

Python port of ``rust/crates/corlinman-gateway/src/routes/models.rs``.
The Rust file ships a 501 stub; the Python port goes a small step
further: when wired with a
:class:`corlinman_providers.registry.ProviderRegistry`, the handler
lists every spec name + the model-prefix defaults so an OpenAI
client (``openai.models.list()``) sees a non-empty catalogue.

Output shape (OpenAI-compatible)::

    {
      "object": "list",
      "data": [
        {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        ...
      ]
    }

Falls back to a 501 envelope when no registry is supplied — matches
the Rust ``not_implemented`` contract so callers without the Python
provider plane still see an honest "not wired yet" signal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

__all__ = [
    "ModelEntry",
    "ModelSource",
    "router",
]


@dataclass(slots=True)
class ModelEntry:
    """One row in the ``/v1/models`` response. Mirrors the OpenAI
    ``Model`` object shape (``id`` + ``object`` + ``owned_by``)."""

    id: str
    owned_by: str = "corlinman"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": self.owned_by,
        }


class ModelSource(Protocol):
    """Minimal surface the route needs to enumerate models.

    Implemented by the boot path against the live
    :class:`corlinman_providers.registry.ProviderRegistry`; tests can
    pass a lambda returning a static list.
    """

    def list_models(self) -> Iterable[ModelEntry]:
        """Return every model id known to the active provider plane."""


def router(source: ModelSource | None = None) -> APIRouter:
    """Build the ``/v1/models`` sub-router.

    :param source: optional :class:`ModelSource`. ``None`` returns the
        Rust-equivalent 501 envelope so production callers know the
        provider plane hasn't been wired yet.
    """
    api = APIRouter()

    @api.get("/v1/models")
    async def list_models() -> JSONResponse:  # noqa: D401
        """Enumerate models exposed by the wired provider registry."""
        if source is None:
            return JSONResponse(
                {
                    "error": "not_implemented",
                    "route": "/v1/models",
                    "message": (
                        "no ProviderRegistry wired; build router(source=...)"
                    ),
                },
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
            )
        entries = [e.to_json() for e in source.list_models()]
        return JSONResponse({"object": "list", "data": entries})

    return api
