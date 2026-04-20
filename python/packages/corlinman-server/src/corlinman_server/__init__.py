"""corlinman-server — gRPC entrypoint for the Python AI plane.

Responsibility: start a ``grpc.aio.server``, register
:class:`corlinman_agent` and :class:`corlinman_embedding` servicers,
install structlog + OpenTelemetry middleware, handle ``SIGTERM`` by
draining + ``sys.exit(143)``. Launched by the Rust gateway as a managed
subprocess (see plan §10).

TODO(M1): implement servicers and register them in :mod:`main`.
"""

from __future__ import annotations

from corlinman_server.main import main

__all__ = ["main"]
