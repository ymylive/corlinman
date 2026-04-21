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
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Final

import grpc.aio
import structlog
from corlinman_grpc import agent_pb2_grpc
from corlinman_providers import AliasEntry, ProviderRegistry, ProviderSpec

from corlinman_server.agent_servicer import CorlinmanAgentServicer
from corlinman_server.middleware import install_tracecontext_interceptor
from corlinman_server.shutdown import GracefulShutdown
from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

logger = structlog.get_logger(__name__)

_DEFAULT_SOCKET: Final[str] = "/tmp/corlinman-py.sock"
_DEFAULT_TCP_ADDR: Final[str] = "127.0.0.1:50051"
_SIGTERM_EXIT_CODE: Final[int] = 143


def _load_config() -> tuple[list[ProviderSpec], dict[str, AliasEntry]]:
    """Read the Python-side config from ``CORLINMAN_PY_CONFIG`` if set.

    The Rust gateway writes a JSON file with ``providers`` + ``aliases``
    blocks translated from its ``config.toml`` before spawning this
    subprocess. The schema is:

    .. code-block:: json

        {
          "providers": [{"name": "...", "kind": "...",
                         "api_key": "...", "base_url": "...",
                         "enabled": true, "params": {...}}, ...],
          "aliases":   {"<alias>": {"provider": "...",
                                    "model": "...",
                                    "params": {...}}, ...}
        }

    When the env var is unset we return empty collections — the registry
    then serves every request via the legacy prefix fallback (M2
    behaviour), which keeps existing deployments working without any
    config-file migration.

    Env-based IPC (vs a second gRPC admin channel) was chosen because the
    Python side already learns about transport config from env vars
    (``CORLINMAN_PY_SOCKET`` / ``CORLINMAN_PY_PORT``); staying inside that
    same channel is the surgical path that doesn't introduce a new
    circular bootstrap problem between the two processes.
    """
    path = os.environ.get("CORLINMAN_PY_CONFIG")
    if not path:
        return [], {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("py_config.load_failed", path=path, error=str(exc))
        return [], {}

    specs: list[ProviderSpec] = []
    for entry in data.get("providers", []) or []:
        try:
            specs.append(ProviderSpec.model_validate(entry))
        except Exception as exc:
            logger.warning("py_config.provider_invalid", entry=entry, error=str(exc))

    aliases: dict[str, AliasEntry] = {}
    raw_aliases: Any = data.get("aliases") or {}
    if isinstance(raw_aliases, dict):
        for name, body in raw_aliases.items():
            try:
                aliases[name] = AliasEntry.model_validate(body)
            except Exception as exc:
                logger.warning(
                    "py_config.alias_invalid", alias=name, error=str(exc)
                )

    return specs, aliases


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
    # S7.T1: install the OTLP tracer + structlog trace_id/span_id binding
    # once per process. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset,
    # and warn-and-continue on any exporter failure.
    init_telemetry()

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
    # Feature C: load the spec-driven provider registry + alias table from
    # the Rust gateway's JSON drop (path in ``CORLINMAN_PY_CONFIG``). Empty
    # config is valid — the servicer falls back to the legacy prefix table.
    specs, aliases = _load_config()
    registry = ProviderRegistry(specs)
    logger.info(
        "providers.registered",
        count=len(specs),
        enabled=sum(1 for s in specs if s.enabled),
        aliases=len(aliases),
    )
    agent_pb2_grpc.add_AgentServicer_to_server(
        CorlinmanAgentServicer(
            provider_resolver=registry.resolve,
            aliases=aliases,
        ),
        server,
    )

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

    shutdown_telemetry()

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
