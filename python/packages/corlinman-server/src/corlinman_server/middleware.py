"""gRPC server middleware — propagate W3C traceparent into structlog contextvars.

Responsibility: read ``traceparent`` from gRPC invocation metadata, bind it
(plus ``request_id``, ``subsystem``) into ``structlog.contextvars`` so every
log emitted while servicing the RPC carries the trace id. Mirrors plan §8 A4.

TODO(M1): implement as a ``grpc.aio.ServerInterceptor`` subclass; bind
``opentelemetry.trace.SpanContext`` alongside.
"""

from __future__ import annotations

from typing import Any

import grpc.aio
import structlog

logger = structlog.get_logger(__name__)


def install_tracecontext_interceptor() -> Any:
    """Return a gRPC interceptor that maps ``traceparent`` → structlog contextvars.

    Currently a pass-through; M1 fills in the real implementation.
    """

    class _TraceContextInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
        async def intercept_service(self, continuation, handler_call_details):  # type: ignore[override]
            # TODO(M1): read handler_call_details.invocation_metadata, bind traceparent
            return await continuation(handler_call_details)

    return _TraceContextInterceptor()
