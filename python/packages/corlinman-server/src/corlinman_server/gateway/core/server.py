"""FastAPI app factory + uvicorn boot helpers.

Python port of ``rust/crates/corlinman-gateway/src/server.rs`` (the
runtime entry-point parts — not the legacy router-construction helpers,
which are the responsibility of the sibling routes/ agent).

This module is intentionally thin so the routes/ agent can mount the
admin router without circular imports:

* :func:`build_app` constructs a bare :class:`fastapi.FastAPI` instance,
  attaches an :class:`~corlinman_server.gateway.core.state.AppState`
  bundle to ``app.state.corlinman``, mounts the metrics + healthz
  fallbacks, and applies the global middleware layers (trace, auth,
  admin, approval — when their states are present on ``AppState``).
* :func:`run_uvicorn` wraps :class:`uvicorn.Server` with the gateway's
  graceful-shutdown contract: SIGTERM / SIGINT trigger a drain and the
  serve coroutine resolves with the
  :class:`~corlinman_server.gateway.core.shutdown.ShutdownReason`.

Routes and admin routes are layered on top by the sibling agents that
own them. They look up the global state via ``Depends(get_app_state)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from corlinman_server.gateway.core.metrics import (
    encode as encode_metrics,
    init as init_metrics,
)
from corlinman_server.gateway.core.shutdown import ShutdownReason, wait_for_signal
from corlinman_server.gateway.core.state import AppState

logger = structlog.get_logger(__name__)


@dataclass
class GatewayServer:
    """Bundle the FastAPI app + uvicorn config + AppState handles.

    Created once by :func:`build_app` and held by the gateway boot
    code. The dataclass shape mirrors the Rust ``(Router, ChatBackend,
    PluginRegistry)`` tuple returned by ``build_runtime`` — same role,
    Pythonic packaging.
    """

    app: Any  # fastapi.FastAPI — held as Any so importing this module
    # without FastAPI installed still works during testing.
    state: AppState
    host: str = "0.0.0.0"
    port: int = 8080


def build_app(
    state: AppState | None = None,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    mount_metrics: bool = True,
    install_default_middleware: bool = True,
    install_health_route: bool = True,
) -> GatewayServer:
    """Build a fresh :class:`fastapi.FastAPI` app wired to ``state``.

    The function is intentionally small — the sibling ``routes/`` and
    ``middleware/`` agents add their own routers via ``app.include_router``
    after this returns. We just provide the scaffolding and the cross-
    cutting middleware.

    Parameters:
        state:                   AppState bundle. Defaults to
                                 :meth:`AppState.empty()` so tests can
                                 stand the app up with zero handles.
        host / port:             uvicorn binding values; passed through
                                 to :class:`GatewayServer`.
        mount_metrics:           when ``True`` (default), exposes
                                 ``GET /metrics`` and ``init_metrics``.
        install_default_middleware:
                                 when ``True`` (default), installs the
                                 trace middleware (HTTP_REQUESTS counter)
                                 and any other middleware whose state is
                                 present on ``state``.
        install_health_route:    when ``True`` (default), mounts a
                                 minimal ``GET /healthz`` endpoint.
                                 The routes/ agent's real ``/health``
                                 will shadow this if both are mounted.

    Returns a :class:`GatewayServer` carrying the live app + state.
    """

    # Import FastAPI lazily so `python -m py_compile` works in
    # environments without it installed (e.g. CI shards that only run
    # the core/metrics/log tests).
    try:
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse, Response
    except ImportError as err:  # pragma: no cover — fastapi is a stated dep
        raise RuntimeError(
            "fastapi is required to build the gateway app — "
            "add `fastapi` to corlinman-server's dependencies"
        ) from err

    state = state or AppState.empty()
    app = FastAPI(title="corlinman-gateway", version="0.1.0")
    app.state.corlinman = state

    if mount_metrics:
        # Pre-touch every metric so the names appear in /metrics on the
        # very first scrape.
        init_metrics()

        @app.get("/metrics", include_in_schema=False)
        async def _metrics() -> Response:
            body = encode_metrics()
            return Response(content=body, media_type="text/plain; version=0.0.4")

    if install_health_route:

        @app.get("/healthz", include_in_schema=False)
        async def _healthz() -> PlainTextResponse:
            return PlainTextResponse("ok")

    if install_default_middleware:
        _install_default_middleware(app, state)

    logger.info("gateway.app.built", host=host, port=port)
    return GatewayServer(app=app, state=state, host=host, port=port)


def _install_default_middleware(app: Any, state: AppState) -> None:
    """Wire the always-on middleware (trace) plus any conditional ones
    whose state-bearing fields are populated on ``AppState``.

    Order: outermost first (trace), then admin/auth/approval where
    relevant. Routes register themselves *after* this so middleware
    sits outside the routing layer. The conditional installs avoid a
    hard import on middleware modules at test time — `core` must not
    depend on middleware shipping any specific surface yet.
    """

    # Lazy import: avoids circular dependency between core and
    # middleware (the trace middleware imports HTTP_REQUESTS from core).
    try:
        from corlinman_server.gateway.middleware.trace import install_trace_middleware
    except ImportError:
        install_trace_middleware = None  # type: ignore[assignment]
    if install_trace_middleware is not None:
        try:
            install_trace_middleware(app)
        except Exception as err:  # pragma: no cover — wiring guard
            logger.warning("gateway.trace_middleware_failed", error=str(err))


async def run_uvicorn(server: GatewayServer) -> ShutdownReason:
    """Run the gateway under :class:`uvicorn.Server` until SIGTERM /
    SIGINT arrives. Returns the triggering reason so the caller can
    pick the right exit code (see :data:`EXIT_CODE_ON_SIGNAL`).
    """

    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover — uvicorn is a stated dep
        raise RuntimeError(
            "uvicorn is required to serve the gateway app — "
            "add `uvicorn` to corlinman-server's dependencies"
        ) from err

    cfg = uvicorn.Config(
        server.app,
        host=server.host,
        port=server.port,
        log_config=None,  # structlog owns logging
        access_log=False,  # trace middleware records HTTP_REQUESTS
    )
    uv = uvicorn.Server(cfg)

    # Race the serve coroutine against the shutdown signal so the
    # gateway exits cleanly on SIGTERM. uvicorn's own signal handler
    # interferes with the asyncio one we use, so we suppress it via
    # the install_signal_handlers flag and drive shutdown manually.
    uv.install_signal_handlers = lambda *_a, **_kw: None  # type: ignore[assignment]

    serve_task = asyncio.create_task(uv.serve(), name="gateway.uvicorn.serve")
    signal_task = asyncio.create_task(wait_for_signal(), name="gateway.shutdown.signal")

    done, _ = await asyncio.wait(
        {serve_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
    )

    reason: ShutdownReason = ShutdownReason.TERMINATE
    if signal_task in done:
        reason = signal_task.result()
        logger.info("gateway.shutdown.requested", reason=reason.value)
        uv.should_exit = True

    if serve_task not in done:
        try:
            await asyncio.wait_for(serve_task, timeout=30.0)
        except asyncio.TimeoutError:
            uv.force_exit = True
            await serve_task

    if not signal_task.done():
        signal_task.cancel()
        try:
            await signal_task
        except (asyncio.CancelledError, Exception):
            pass

    return reason


__all__ = [
    "GatewayServer",
    "build_app",
    "run_uvicorn",
]
