"""HTTP route modules mounted by :func:`register.build_app_router`.

Python port of ``rust/crates/corlinman-gateway/src/routes/*.rs``. Each
top-level Rust file becomes one Python sub-module here that exposes:

* ``router(...)`` — :class:`fastapi.APIRouter` factory matching the
  Rust ``axum::Router::new()`` per-file pattern.
* Optionally a state dataclass / Protocol the caller injects via
  FastAPI's dependency-overrides surface (the Python analogue of
  ``Router::with_state``).

The composition root lives in :mod:`.register` so application boot
code can mount every sub-router in one call::

    from fastapi import FastAPI
    from corlinman_server.gateway.routes.register import (
        GatewayState, build_app_router,
    )

    state = GatewayState(...)
    app = FastAPI()
    app.include_router(build_app_router(state))

NOTE: this submodule is import-light. Each route module's heavy
sibling deps (``corlinman_memory_host``, ``corlinman_canvas``, …)
are imported lazily inside the handler so missing-package failures
surface as 503s on the affected route rather than aborting boot of
the whole gateway.
"""

from __future__ import annotations

from corlinman_server.gateway.routes import (
    canvas,
    channels,
    chat,
    chat_approve,
    embeddings,
    health,
    memory,
    metrics,
    models,
    plugin_callback,
    register,
)

__all__ = [
    "canvas",
    "channels",
    "chat",
    "chat_approve",
    "embeddings",
    "health",
    "memory",
    "metrics",
    "models",
    "plugin_callback",
    "register",
]
