"""``/v1/memory/*`` — HTTP MemoryHost protocol.

Python port of ``rust/crates/corlinman-gateway/src/routes/memory.rs``.
Mirrors the Rust contract:

* ``POST /v1/memory/query``       — top-k query, body = ``MemoryQuery``.
* ``POST /v1/memory/upsert``      — write doc, body = ``MemoryDoc``.
* ``GET  /v1/memory/docs/{id}``   — read by id.
* ``DELETE /v1/memory/docs/{id}`` — delete by id.
* ``GET  /v1/memory/health``      — adapter health.

Backed by any :class:`corlinman_memory_host.MemoryHost` implementation
(local SQLite, remote HTTP, federated). The route surface is
storage-agnostic — boot code picks the adapter and hands the result
to :class:`MemoryState`.

The Rust route family is rooted at ``/memory/*``; we mount the canonical
modern URLs at ``/v1/memory/*`` and keep the legacy ``/memory/*`` paths
as aliases so the Python curator clients (which were authored against
the Rust shape) still work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response

if TYPE_CHECKING:
    from corlinman_memory_host import MemoryHost

__all__ = ["MemoryState", "router"]


@dataclass(slots=True)
class MemoryState:
    """Shared state injected into every memory handler."""

    host: MemoryHost


def _lazy_imports() -> tuple[type, type, type]:
    """Resolve the W2 :mod:`corlinman_memory_host` types lazily.

    Keeping the import here means a server boot without the W2
    package installed can still construct the rest of the gateway
    router; only ``/v1/memory/*`` handlers raise when called.
    """
    from corlinman_memory_host import (  # noqa: PLC0415
        MemoryDoc,
        MemoryHostError,
        MemoryQuery,
    )

    return MemoryDoc, MemoryHostError, MemoryQuery


def _storage_error(err: Exception) -> JSONResponse:
    """Wrap an upstream :class:`MemoryHostError` (or anything else
    surfacing from the adapter) into the Rust-compatible
    ``storage_error`` envelope.
    """
    return JSONResponse(
        {
            "error": "storage_error",
            "message": str(err),
        },
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _hit_to_json(hit: Any) -> dict[str, Any]:
    """Best-effort serialiser for MemoryHit-like values.

    Adapters return :class:`corlinman_memory_host.MemoryHit`, but
    tests may inject plain dicts — accept either.
    """
    if hasattr(hit, "to_json"):
        return hit.to_json()
    if isinstance(hit, dict):
        return hit
    return {
        "id": getattr(hit, "id", ""),
        "content": getattr(hit, "content", ""),
        "score": getattr(hit, "score", 0.0),
        "source": getattr(hit, "source", ""),
        "metadata": getattr(hit, "metadata", None),
    }


def router(state: MemoryState) -> APIRouter:
    """Build the ``/v1/memory/*`` sub-router (plus legacy aliases)."""
    api = APIRouter()

    async def _query(request: Request) -> JSONResponse:
        try:
            _, MemoryHostError, MemoryQuery = _lazy_imports()
            raw = await request.json()
            query = MemoryQuery.from_json(raw)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "message": f"could not decode MemoryQuery: {exc}",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            hits = await state.host.query(query)
        except MemoryHostError as exc:
            return _storage_error(exc)
        return JSONResponse({"hits": [_hit_to_json(h) for h in hits]})

    async def _upsert(request: Request) -> JSONResponse:
        try:
            MemoryDoc, MemoryHostError, _ = _lazy_imports()
            raw = await request.json()
            if not isinstance(raw, dict):
                raise TypeError("expected JSON object")
            doc = MemoryDoc(
                content=str(raw.get("content", "")),
                metadata=raw.get("metadata"),
                namespace=raw.get("namespace"),
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "message": f"could not decode MemoryDoc: {exc}",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            doc_id = await state.host.upsert(doc)
        except MemoryHostError as exc:
            return _storage_error(exc)
        return JSONResponse({"id": doc_id})

    async def _get_doc(id: str) -> Response:  # noqa: A002 — match Rust naming
        try:
            _, MemoryHostError, _ = _lazy_imports()
            hit = await state.host.get(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(exc)
        if hit is None:
            return JSONResponse(
                {"error": "not_found", "resource": "memory_doc", "id": id},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return JSONResponse(_hit_to_json(hit))

    async def _delete_doc(id: str) -> Response:  # noqa: A002
        try:
            _, MemoryHostError, _ = _lazy_imports()
            await state.host.delete(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _health() -> Response:
        try:
            _, MemoryHostError, _ = _lazy_imports()
            health = await state.host.health()
        except Exception as exc:  # noqa: BLE001
            return _storage_error(exc)

        # ``HealthStatus`` has a ``kind`` enum + ``detail`` field per
        # the W2 port. Discriminate on ``kind.value`` so the wire shape
        # matches the Rust ``HealthStatus`` JSON byte-for-byte.
        kind = getattr(getattr(health, "kind", None), "value", "ok")
        detail = getattr(health, "detail", "")
        if kind == "ok":
            return JSONResponse({"status": "ok"})
        if kind == "degraded":
            return JSONResponse(
                {"status": "degraded", "message": detail},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse(
            {"status": "down", "message": detail},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Canonical /v1 routes
    api.add_api_route("/v1/memory/query", _query, methods=["POST"])
    api.add_api_route("/v1/memory/upsert", _upsert, methods=["POST"])
    api.add_api_route("/v1/memory/docs/{id}", _get_doc, methods=["GET"])
    api.add_api_route("/v1/memory/docs/{id}", _delete_doc, methods=["DELETE"])
    api.add_api_route("/v1/memory/health", _health, methods=["GET"])

    # Legacy Rust-compatible aliases
    api.add_api_route("/memory/query", _query, methods=["POST"])
    api.add_api_route("/memory/upsert", _upsert, methods=["POST"])
    api.add_api_route("/memory/docs/{id}", _get_doc, methods=["GET"])
    api.add_api_route("/memory/docs/{id}", _delete_doc, methods=["DELETE"])
    api.add_api_route("/memory/health", _health, methods=["GET"])

    return api
