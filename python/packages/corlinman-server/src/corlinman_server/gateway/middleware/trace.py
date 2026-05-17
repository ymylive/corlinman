"""Trace middleware: HTTP request metrics + (future) traceparent propagation.

Python port of ``rust/crates/corlinman-gateway/src/middleware/trace.rs``.

The Rust port currently only implements the
``corlinman_http_requests_total`` counter (with a TODO for traceparent
extraction). We mirror that exactly: every response increments the
counter, labelled by the route template (so cardinality stays bounded)
and the HTTP status code.

FastAPI exposes the matched route template via ``request.scope["route"]``
once routing has run; we read it from there in the same way axum's
``MatchedPath`` extension works. Unmatched requests fall back to the
raw URL path — matches the Rust behaviour for 404s.

The traceparent propagation lands in a follow-up alongside the
``corlinman_server.middleware.install_tracecontext_interceptor`` gRPC
counterpart so HTTP + gRPC share one extraction path.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from corlinman_server.gateway.core.metrics import HTTP_REQUESTS


class TraceMiddleware(BaseHTTPMiddleware):
    """Records one ``corlinman_http_requests_total`` sample per response.

    ``route`` uses Starlette's matched route ``path`` (e.g.
    ``/v1/chat/completions``, ``/admin/health``) so cardinality is
    bounded; unmatched paths fall back to the raw URI path.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        route = _route_template(request)
        status = str(response.status_code)
        HTTP_REQUESTS.labels(route=route, status=status).inc()
        return response


def _route_template(request: Request) -> str:
    """Resolve the matched route template, falling back to the raw path.

    Starlette stashes the matched ``Route`` instance on
    ``request.scope["route"]`` after routing — its ``path`` attribute
    is the template used at mount time (e.g. ``/v1/chat/{model}``)."""

    route = request.scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if path:
            return str(path)
    return request.url.path


def install_trace_middleware(app: Any) -> None:
    """Attach :class:`TraceMiddleware` to ``app``. Called once from
    :func:`corlinman_server.gateway.core.server.build_app`."""

    app.add_middleware(TraceMiddleware)


__all__ = [
    "TraceMiddleware",
    "install_trace_middleware",
]
