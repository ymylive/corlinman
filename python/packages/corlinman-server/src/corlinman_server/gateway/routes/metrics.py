"""``GET /metrics`` — Prometheus scrape endpoint.

Python port of ``rust/crates/corlinman-gateway/src/routes/metrics.rs``.
Mirrors the Rust contract:

* Output is the Prometheus text-exposition v0.0.4 format so any
  scraper consumes it directly.
* Content-Type is ``text/plain; version=0.0.4`` (matches the Rust
  header).
* Metric definitions + the registry live elsewhere; this module
  just renders the active registry on demand.

The default registry resolution uses
:func:`prometheus_client.generate_latest`'s argument-less form which
serialises the global ``REGISTRY``. Callers that maintain their own
:class:`prometheus_client.CollectorRegistry` can inject it via
:func:`router(registry=...)`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

__all__ = ["CONTENT_TYPE", "router"]

#: Content-Type header Prometheus expects on the scrape response.
#: Mirrors the Rust handler's ``"text/plain; version=0.0.4"`` literal.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def router(registry: Any | None = None) -> APIRouter:
    """Build the ``/metrics`` sub-router.

    :param registry: optional :class:`prometheus_client.CollectorRegistry`
        instance. ``None`` falls through to the global registry so the
        usual ``prometheus_client.Counter(...)`` declarations show up
        without any extra wiring.
    """
    api = APIRouter()

    # Import lazily so callers that scrape an empty default registry
    # don't pay the prometheus_client import cost until first scrape.
    from prometheus_client import (  # noqa: PLC0415 — lazy import is deliberate
        CONTENT_TYPE_LATEST,
        REGISTRY,
        generate_latest,
    )

    target_registry = registry if registry is not None else REGISTRY
    # The Rust handler ships a hard-coded content type; prometheus_client
    # exposes the canonical value as a constant. Prefer that when the
    # default registry is in use — third-party tooling may have negotiated
    # a different one (e.g. OpenMetrics).
    media_type = (
        CONTENT_TYPE_LATEST if registry is None else CONTENT_TYPE
    )

    @api.get("/metrics")
    async def metrics_handler() -> Response:  # noqa: D401
        """Serialise the active Prometheus registry."""
        body = generate_latest(target_registry)
        return Response(content=body, media_type=media_type)

    return api
