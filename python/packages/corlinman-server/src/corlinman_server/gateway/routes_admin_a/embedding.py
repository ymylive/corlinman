"""``/admin/embedding*`` — embedding provider configuration + benchmark.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/embedding.rs``.

Three routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/embedding``           — current ``[embedding]`` section
  or 404 ``not_configured`` when the bootstrapper hasn't installed one.
* ``POST /admin/embedding``           — upsert. Validates the required
  fields, then hands the new dict to the bootstrapper's
  ``embedding_writer`` callback for persistence.
* ``POST /admin/embedding/benchmark`` — passthrough to the Python
  sidecar at ``state.py_admin_url``. Connection failures surface as
  503 ``python_sidecar_unavailable`` so the UI can distinguish "Python
  offline" from validation errors.

The Rust side validates ``params`` against a per-provider JSON-schema
extracted from the registered ``ProviderKind``. The Python port
deliberately keeps ``params`` opaque (``dict[str, Any]``) — the
provider-kind schema lives in the parallel ``providers`` Rust module
which is owned by ``routes_admin_b``. The benchmark sidecar already
runs its own provider-side validation, so we don't lose defence in
depth. A follow-up can wire a schema validator through ``AdminState``
when ``routes_admin_b.providers`` lands.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class EmbeddingView(BaseModel):
    """``GET /admin/embedding`` response. Mirrors the Rust
    ``EmbeddingView`` struct."""

    provider: str
    model: str
    dimension: int
    enabled: bool
    params: dict[str, Any] = Field(default_factory=dict)
    # The Rust side computes the params JSON-schema from the registered
    # provider-kind. The Python admin slice ships an empty object
    # placeholder until ``routes_admin_b.providers`` exposes a schema
    # registry that this module can call into.
    params_schema: dict[str, Any] = Field(default_factory=dict)


class EmbeddingUpsert(BaseModel):
    """``POST /admin/embedding`` request body."""

    provider: str
    model: str
    dimension: int
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class BenchmarkBody(BaseModel):
    """``POST /admin/embedding/benchmark`` request body."""

    samples: list[str]
    dimension: int | None = None
    params: dict[str, Any] | None = None
    # Unused — retained for backward-compat with earlier UI builds.
    limit: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BENCHMARK_TIMEOUT_SECS: float = 60.0


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": code, "message": message},
    )


def _render_view(cfg: dict[str, Any]) -> EmbeddingView:
    """Project the live embedding-config dict into the wire shape."""
    return EmbeddingView(
        provider=str(cfg.get("provider", "")),
        model=str(cfg.get("model", "")),
        dimension=int(cfg.get("dimension", 0)),
        enabled=bool(cfg.get("enabled", True)),
        params=dict(cfg.get("params", {}) or {}),
        params_schema={},
    )


async def _invoke_writer(writer: Any, snapshot: dict[str, Any]) -> None:
    """Call ``state.embedding_writer`` with the new snapshot.

    Synchronous + async callbacks both supported — mirrors the
    ``channels_writer`` convention in :mod:`channels`.
    """
    ret = writer(snapshot)
    if inspect.isawaitable(ret):
        await ret


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/embedding*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/embedding",
        response_model=EmbeddingView,
        summary="Current embedding section",
    )
    async def get_embedding(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> EmbeddingView:
        cfg = state.embedding_config
        if cfg is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_configured",
                    "message": "no [embedding] section configured",
                },
            )
        return _render_view(cfg)

    @r.post(
        "/admin/embedding",
        response_model=EmbeddingView,
        summary="Upsert the [embedding] section",
    )
    async def post_embedding(
        body: EmbeddingUpsert,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> EmbeddingView:
        # Field validation — match the Rust contract's 400 envelopes.
        if not body.provider.strip():
            raise _bad_request("invalid_provider", "provider must be non-empty")
        if not body.model.strip():
            raise _bad_request("invalid_model", "model must be non-empty")
        if body.dimension == 0:
            raise _bad_request("invalid_dimension", "dimension must be > 0")

        writer = state.embedding_writer
        if writer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "config_path_unset",
                    "message": "gateway booted without an embedding config writer",
                },
            )

        new_cfg: dict[str, Any] = {
            "provider": body.provider,
            "model": body.model,
            "dimension": body.dimension,
            "enabled": body.enabled,
            "params": dict(body.params),
        }

        try:
            await _invoke_writer(writer, new_cfg)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc

        # Refresh the live snapshot so subsequent GETs see the write.
        state.embedding_config = new_cfg
        return _render_view(new_cfg)

    @r.post(
        "/admin/embedding/benchmark",
        summary="Embedding benchmark (passthrough to Python sidecar)",
    )
    async def post_benchmark(
        body: BenchmarkBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> JSONResponse:
        if not body.samples:
            raise _bad_request("invalid_samples", "samples must be non-empty")
        if len(body.samples) > 20:
            raise _bad_request(
                "too_many_samples",
                "samples is capped at 20 entries per benchmark call",
            )

        base = state.py_admin_url.rstrip("/")
        url = f"{base}/embedding/benchmark"

        payload: dict[str, Any] = {"samples": list(body.samples)}
        if body.dimension is not None:
            payload["dimension"] = body.dimension
        if body.params is not None:
            payload["params"] = dict(body.params)

        try:
            async with httpx.AsyncClient(timeout=BENCHMARK_TIMEOUT_SECS) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # The Rust side maps every reqwest connect/timeout failure to
            # 503 ``python_sidecar_unavailable`` so the UI can render a
            # "Python plane is down" banner distinct from validation
            # errors. We mirror that here.
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": "python_sidecar_unavailable",
                    "message": str(exc),
                },
            )

        # Pass the sidecar's JSON through verbatim (success or error) so
        # the contract its handlers emit reaches the caller unchanged.
        try:
            json_body = resp.json()
        except ValueError:
            json_body = {"raw": resp.text}
        return JSONResponse(status_code=resp.status_code, content=json_body)

    return r


__all__ = [
    "BenchmarkBody",
    "EmbeddingUpsert",
    "EmbeddingView",
    "router",
]
