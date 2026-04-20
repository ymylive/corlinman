"""``main()`` — boot the grpc.aio server and handle SIGTERM → exit 143.

Usage (installed as console script ``corlinman-python-server``)::

    corlinman-python-server

Defaults to a Unix domain socket at ``$CORLINMAN_PY_SOCKET`` or
``/tmp/corlinman-py.sock`` so the Rust gateway can co-locate without a TCP
port. Servicers are registered below — each is currently a no-op stub
until M1/M2.

TODO(M1): register the Agent / Embedding servicers once proto stubs exist
in :mod:`corlinman_grpc`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Final

import grpc.aio
import structlog
from corlinman_grpc import agent_pb2_grpc

from corlinman_server.agent_servicer import CorlinmanAgentServicer
from corlinman_server.middleware import install_tracecontext_interceptor
from corlinman_server.shutdown import GracefulShutdown

logger = structlog.get_logger(__name__)

_DEFAULT_SOCKET: Final[str] = "/tmp/corlinman-py.sock"
_DEFAULT_TCP_ADDR: Final[str] = "127.0.0.1:50051"
_SIGTERM_EXIT_CODE: Final[int] = 143


def _bind_address() -> str:
    """Resolve the gRPC bind address from env.

    Precedence:
      1. ``CORLINMAN_PY_SOCKET`` — Unix domain socket path.
      2. ``CORLINMAN_PY_ADDR``   — explicit ``host:port`` (e.g. ``127.0.0.1:50051``).
      3. ``CORLINMAN_PY_PORT``   — port only, bound to ``127.0.0.1``.
      4. default Unix socket at ``/tmp/corlinman-py.sock``.
    """
    sock = os.environ.get("CORLINMAN_PY_SOCKET")
    if sock:
        return f"unix://{sock}"
    addr = os.environ.get("CORLINMAN_PY_ADDR")
    if addr:
        return addr
    port = os.environ.get("CORLINMAN_PY_PORT")
    if port:
        return f"127.0.0.1:{port}"
    return f"unix://{_DEFAULT_SOCKET}"


async def _serve() -> int:
    """Run the server until SIGTERM / SIGINT is received.

    Returns the process exit code (143 on SIGTERM, 0 on clean shutdown).
    """
    shutdown = GracefulShutdown()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.request, sig.name)

    server = grpc.aio.server(
        interceptors=[install_tracecontext_interceptor()],
        options=[
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ],
    )

    # M2: real Agent servicer drives corlinman_agent.ReasoningLoop.
    # TODO(M3): register EmbeddingServiceServicer once implemented:
    #   from corlinman_grpc import embedding_pb2_grpc
    #   embedding_pb2_grpc.add_EmbeddingServicer_to_server(EmbeddingServicer(), server)
    agent_pb2_grpc.add_AgentServicer_to_server(CorlinmanAgentServicer(), server)

    bind = _bind_address()
    server.add_insecure_port(bind)
    logger.info("grpc.server.start", bind=bind)
    print(f"corlinman-server ready (Agent servicer registered) — bind={bind}", flush=True)

    await server.start()
    reason = await shutdown.wait()
    logger.info("grpc.server.shutdown", reason=reason)

    # 5s grace for in-flight RPCs, then force close.
    await server.stop(grace=5.0)
    logger.info("grpc.server.stopped")

    return _SIGTERM_EXIT_CODE if reason == "SIGTERM" else 0


def main() -> None:
    """Entrypoint wrapper — runs :func:`_serve` and exits with its code."""
    try:
        code = asyncio.run(_serve())
    except KeyboardInterrupt:
        code = _SIGTERM_EXIT_CODE
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    main()
