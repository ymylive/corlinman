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

from corlinman_server.admin_sidecar import start_admin_sidecar
from corlinman_server.agent_servicer import CorlinmanAgentServicer
from corlinman_server.middleware import install_tracecontext_interceptor
from corlinman_server.shutdown import GracefulShutdown
from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

logger = structlog.get_logger(__name__)

_DEFAULT_SOCKET: Final[str] = "/tmp/corlinman-py.sock"
_DEFAULT_TCP_ADDR: Final[str] = "127.0.0.1:50051"
_SIGTERM_EXIT_CODE: Final[int] = 143


class _ReloadingProviderResolver:
    """File-mtime-aware wrapper around :class:`ProviderRegistry`.

    The Rust gateway re-writes ``$CORLINMAN_PY_CONFIG`` after every admin
    mutation (``POST /admin/{config,providers,embedding,models}``). This
    wrapper checks the file mtime before each resolve; on change it
    rebuilds the underlying registry + alias table atomically.

    The surface matches what :class:`CorlinmanAgentServicer` expects of a
    resolver: callable as ``(alias_or_model=..., aliases=...)`` returning
    ``(provider, upstream_model, merged_params)``. The ``aliases=`` kwarg
    passed by the servicer is *ignored* — this class owns the live alias
    map and the servicer's copy is a no-op pass-through (kept for signature
    compatibility).
    """

    def __init__(self, path: str | None) -> None:
        self._path = path
        self._mtime: float | None = None
        self._registry = ProviderRegistry([])
        self._aliases: dict[str, AliasEntry] = {}
        if path:
            self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        """Re-read the JSON file if its mtime moved. Logs on first load +
        every subsequent reload — the success-criterion grep for
        ``providers.registered`` in the doc walks this exact event."""
        if not self._path:
            return
        try:
            mtime = Path(self._path).stat().st_mtime
        except OSError:
            # File vanished — keep whatever registry we had. A subsequent
            # write will land a fresh mtime and we'll pick it up.
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        is_first_load = self._mtime is None
        specs, aliases = _load_config()
        self._registry = ProviderRegistry(specs)
        self._aliases = aliases
        self._mtime = mtime
        event = "providers.registered" if is_first_load else "providers.reloaded"
        logger.info(
            event,
            count=len(specs),
            enabled=sum(1 for s in specs if s.enabled),
            aliases=len(aliases),
        )

    @property
    def aliases(self) -> dict[str, AliasEntry]:
        """Snapshot of the current alias map."""
        return dict(self._aliases)

    def __call__(
        self,
        alias_or_model: str,
        aliases: Any = None,
    ) -> tuple[Any, str, dict[str, Any]]:
        _ = aliases  # servicer-supplied aliases ignored; we own the live map
        self._reload_if_changed()
        return self._registry.resolve(
            alias_or_model=alias_or_model, aliases=self._aliases
        )


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
    #
    # The resolver is a file-mtime-aware wrapper so admin writes on the
    # Rust side (which rewrite py-config.json atomically) propagate here
    # without a process restart.
    if os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER") is not None:
        # Test smoke path: leave provider_resolver unset so the Agent
        # servicer activates its offline mock provider instead of falling
        # through to legacy real-provider prefix matching.
        logger.info("providers.registered", count=0, enabled=0, aliases=0)
        agent_servicer = CorlinmanAgentServicer()
    else:
        py_config_path = os.environ.get("CORLINMAN_PY_CONFIG")
        resolver = _ReloadingProviderResolver(py_config_path)
        if py_config_path is None:
            # No config handshake → legacy prefix fallback for every resolve.
            # Log zeros so the boot-time grep target stays consistent.
            logger.info("providers.registered", count=0, enabled=0, aliases=0)
        agent_servicer = CorlinmanAgentServicer(
            provider_resolver=resolver,
            aliases=resolver.aliases,
        )
    agent_pb2_grpc.add_AgentServicer_to_server(agent_servicer, server)

    # Feature C last-mile: localhost HTTP sidecar backs
    # ``POST /admin/embedding/benchmark`` on the Rust gateway. Failure
    # to bind is warn-and-continue — the rest of the process keeps working.
    sidecar = start_admin_sidecar(asyncio.get_running_loop())

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

    if sidecar is not None:
        sidecar.shutdown()

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
