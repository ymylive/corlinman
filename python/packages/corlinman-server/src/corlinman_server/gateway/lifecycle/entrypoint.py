"""``corlinman-gateway`` console-script entrypoint.

Python port of ``rust/crates/corlinman-gateway/src/main.rs``.

Boot sequence (parity with the Rust binary, simplified for an
ASGI-on-uvicorn deployment):

1. Parse ``--config <path>`` / ``--host`` / ``--port`` / ``--data-dir``
   from the CLI (also accepts ``CORLINMAN_CONFIG`` / ``BIND`` / ``PORT``
   / ``CORLINMAN_DATA_DIR`` env vars to keep deployments that already
   set them working).
2. Initialise telemetry (OTLP exporter + structlog binding) via
   :mod:`corlinman_server.telemetry`.
3. Run the one-shot legacy data-file migration when the config gates it.
4. Emit the Rust→Python config handshake JSON drop (so any in-process
   consumer that watches ``CORLINMAN_PY_CONFIG`` sees a non-empty
   registry from the first request).
5. Build the FastAPI :class:`fastapi.FastAPI` app via :func:`build_app`
   (lazy-imports the routes/middleware/core/grpc submodules being landed
   by sibling agents).
6. Run uvicorn programmatically with graceful shutdown wired to
   SIGTERM/SIGINT.

Sibling-module wiring
---------------------

Other agents own ``gateway/core/``, ``gateway/middleware/``,
``gateway/routes/``, ``gateway/grpc/``, ``gateway/services/``,
``gateway/evolution/``. Their modules may not exist when this file is
imported, so the FastAPI app factory uses :func:`_lazy_import` to swallow
:class:`ImportError` and log the missing wiring. The expected contract:

* ``gateway.core.AppState.build(config=...)`` → returns an ``AppState``
  bundle (analogue of the Rust ``AppState`` struct).
* ``gateway.middleware.install(app, state)`` → installs every
  cross-cutting middleware (tracing, approval gate, tenant resolution).
* ``gateway.routes.mount(app, state)`` → mounts every HTTP route
  (chat / admin / channels / canvas / …).
* ``gateway.grpc.serve_placeholder_in_background(state, cancel)`` →
  spawns the Rust→Python placeholder UDS server (returns an awaitable).
* ``gateway.services.bootstrap(state)`` → spins up channel adapters,
  plugin supervisors, hot reloaders.
* ``gateway.evolution.bootstrap(state)`` → spawns the evolution observer
  + scheduler.

Each hook is best-effort: a missing sibling logs ``warning`` and the
gateway boots in degraded mode so a partial port can still serve.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.legacy_migration import (
    migrate_legacy_data_files,
)
from corlinman_server.gateway.lifecycle.py_config import (
    default_py_config_path,
    write_py_config_sync,
)

logger = structlog.get_logger(__name__)

#: Mirrors ``corlinman_gateway::main::resolve_addr`` — same defaults so a
#: deployment-script that sets ``PORT`` / ``BIND`` against the Rust
#: binary keeps working against the Python port.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 6005
SIGTERM_EXIT_CODE: int = 143


# ---------------------------------------------------------------------------
# Lazy-import helper for sibling modules
# ---------------------------------------------------------------------------


def _lazy_import(dotted: str) -> Any | None:
    """Import ``dotted`` and return the module; ``None`` on ImportError.

    The siblings populated by parallel agents may not exist when this
    file is imported. Swallowing ``ImportError`` lets ``build_app`` boot
    in degraded mode without leaking partial-port state into a startup
    crash.
    """
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:
        logger.warning(
            "gateway.sibling_missing",
            module=dotted,
            error=str(exc),
            detail=(
                "sibling submodule not present; gateway will boot in "
                "degraded mode without it"
            ),
        )
        return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _resolve_config_path(cli_value: str | None) -> Path | None:
    """``--config`` > ``$CORLINMAN_CONFIG`` > ``None``. Mirrors the
    Rust ``main::load_config`` precedence."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_CONFIG")
    if env:
        return Path(env)
    return None


def _resolve_data_dir(cli_value: str | None) -> Path:
    """``--data-dir`` > ``$CORLINMAN_DATA_DIR`` > ``~/.corlinman`` >
    ``./.corlinman``. Mirrors ``corlinman_gateway::server::resolve_data_dir``."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    try:
        return Path.home() / ".corlinman"
    except (RuntimeError, OSError):
        return Path(".corlinman")


def _load_config(path: Path | None) -> Any | None:
    """Best-effort config load.

    Sibling agents populate ``gateway.core.config`` (a Python port of
    ``corlinman_core::config::Config``). We import lazily so the
    entrypoint module stays importable without it. A missing file or
    missing loader returns ``None`` and the gateway boots with whatever
    defaults the downstream modules carry.
    """
    if path is None:
        return None
    if not path.exists():
        logger.warning("gateway.config.missing", path=str(path))
        return None
    core_config = _lazy_import("corlinman_server.gateway.core.config")
    if core_config is None:
        logger.warning(
            "gateway.config.no_loader",
            path=str(path),
            detail="gateway.core.config not present; skipping load",
        )
        return None
    loader: Callable[[Path], Any] | None = (
        getattr(core_config, "load_from_path", None)
        or getattr(core_config, "Config", None)
    )
    if loader is None:
        logger.warning("gateway.config.no_loader_symbol", path=str(path))
        return None
    try:
        cfg = loader(path)  # type: ignore[misc]
        # ``Config(path)`` returning a class is fine — the duck-typed
        # downstream code only reads attributes off whatever we hand it.
    except Exception as exc:
        logger.warning(
            "gateway.config.load_failed", path=str(path), error=str(exc)
        )
        return None
    logger.info("gateway.config.loaded", path=str(path))
    return cfg


def _should_run_legacy_migration(cfg: Any | None) -> bool:
    """Mirror the Rust gate: ``[tenants].enabled && [tenants].migrate_legacy_paths``.

    Default off — pre-Phase-4 deployments keep their flat layout unless
    the operator opts in.
    """
    if cfg is None:
        return False
    tenants = getattr(cfg, "tenants", None)
    if tenants is None and isinstance(cfg, dict):
        tenants = cfg.get("tenants")
    if tenants is None:
        return False
    enabled = (
        getattr(tenants, "enabled", None)
        if not isinstance(tenants, dict)
        else tenants.get("enabled")
    )
    migrate = (
        getattr(tenants, "migrate_legacy_paths", None)
        if not isinstance(tenants, dict)
        else tenants.get("migrate_legacy_paths")
    )
    return bool(enabled) and bool(migrate)


def _emit_py_config_drop(cfg: Any | None) -> None:
    """Best-effort write of the JSON handshake file.

    No-op when ``cfg`` is ``None`` — there's nothing to render and the
    Python AI plane falls back to the legacy prefix table in that case
    (matches the Rust behaviour).
    """
    if cfg is None:
        return
    target = Path(
        os.environ.get("CORLINMAN_PY_CONFIG") or str(default_py_config_path())
    )
    try:
        write_py_config_sync(cfg, target)
        logger.info("gateway.py_config.written", path=str(target))
    except Exception as exc:
        logger.warning(
            "gateway.py_config.write_failed",
            path=str(target),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# AppState bridge
# ---------------------------------------------------------------------------


def _build_state(cfg: Any | None, data_dir: Path) -> Any:
    """Construct the shared ``AppState`` bundle.

    Delegates to ``gateway.core.AppState`` when available; falls back to
    a minimal :class:`_DegradedAppState` so degraded-mode boots still
    have *some* object to pass into route handlers / tests.
    """
    core = _lazy_import("corlinman_server.gateway.core")
    if core is not None:
        builder: Any = (
            getattr(core, "build_app_state", None)
            or getattr(core, "AppState", None)
        )
        if builder is not None:
            try:
                return builder(config=cfg, data_dir=data_dir)  # type: ignore[misc]
            except TypeError:
                # Builders that take positional args, or only ``config=``,
                # or no args at all — fall through, try a less ambitious
                # signature, finally drop to the degraded stub.
                try:
                    return builder(cfg, data_dir)  # type: ignore[misc]
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.state.builder_failed",
                        builder=type(builder).__name__,
                        error=str(exc),
                    )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.state.builder_failed",
                    builder=type(builder).__name__,
                    error=str(exc),
                )
    return _DegradedAppState(config=cfg, data_dir=data_dir)


class _DegradedAppState:
    """Minimal stand-in used when ``gateway.core`` isn't ported yet.

    Carries just enough state for the placeholder resolvers + a basic
    ``/health`` route to function. Sibling agents will replace this with
    the real ``AppState`` bundle.
    """

    __slots__ = ("config", "data_dir")

    def __init__(self, *, config: Any | None, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"_DegradedAppState(data_dir={self.data_dir!r})"


# ---------------------------------------------------------------------------
# Routes composition (parallel-agent contracts diverge per submodule)
# ---------------------------------------------------------------------------


def _mount_routes(app: Any, state: Any) -> None:
    """Mount every gateway routes submodule onto ``app``.

    Each W4 submodule exposes a different composition surface; this
    helper wires them all in one place so ``build_app`` stays compact:

    * ``routes.register.build_app_router(state)`` — top-level / endpoint set
    * ``routes_voice.mod.router(voice_state)`` — /v1/voice WebSocket
    * ``routes_admin_a.build_router()`` — admin A bundle (9 sub-routers)
    * ``routes_admin_b.build_router()`` — admin B bundle (13 sub-routers)

    Missing submodules log a warning and the gateway continues to boot
    in degraded mode (so a partial port still serves health checks).
    """
    routes_top = _lazy_import("corlinman_server.gateway.routes.register")
    if routes_top is not None:
        try:
            gateway_state_cls = getattr(routes_top, "GatewayState", None)
            build_app_router = getattr(routes_top, "build_app_router", None)
            if gateway_state_cls is not None and build_app_router is not None:
                # GatewayState is a dataclass of duck-typed optional deps;
                # we hand it the AppState handle so route handlers can
                # downcast as they need.
                gw_state = (
                    gateway_state_cls(app_state=state)
                    if hasattr(gateway_state_cls, "__dataclass_fields__")
                    and "app_state" in gateway_state_cls.__dataclass_fields__
                    else gateway_state_cls()
                )
                app.include_router(build_app_router(gw_state))
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes.top.mount_failed", error=str(exc))

    routes_voice_mod = _lazy_import("corlinman_server.gateway.routes_voice.mod")
    if routes_voice_mod is not None:
        try:
            voice_router = routes_voice_mod.router()
            app.include_router(voice_router)
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_voice.mount_failed", error=str(exc))

    admin_a = _lazy_import("corlinman_server.gateway.routes_admin_a")
    if admin_a is not None:
        try:
            app.include_router(admin_a.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_a.mount_failed", error=str(exc))

    admin_b = _lazy_import("corlinman_server.gateway.routes_admin_b")
    if admin_b is not None:
        try:
            app.include_router(admin_b.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_b.mount_failed", error=str(exc))


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Build the FastAPI app + AppState and wire every sibling module.

    Returns a :class:`fastapi.FastAPI` instance ready to be served by
    uvicorn. The app exposes ``app.state.corlinman_state`` for tests /
    middleware that need the shared handle.

    Sibling agents that haven't landed yet just log a warning and skip
    their wiring step — the app still starts (in degraded mode) so the
    integration step can roll forward iteratively.
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover — fastapi is a runtime dep
        raise RuntimeError(
            "fastapi is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    cfg = _load_config(config_path)
    resolved_data_dir = data_dir or _resolve_data_dir(None)

    # Phase 4 W1 4-1A Item 5: one-shot legacy data-file migration. Gated
    # on tenants config; default-off for back-compat.
    if _should_run_legacy_migration(cfg):
        try:
            migrate_legacy_data_files(resolved_data_dir)
        except OSError as exc:
            logger.warning(
                "gateway.legacy_migration.failed",
                data_dir=str(resolved_data_dir),
                error=str(exc),
            )

    # Feature C last-mile: re-emit the JSON drop so any in-process
    # consumer that mtime-watches CORLINMAN_PY_CONFIG sees a fully-formed
    # registry from boot.
    _emit_py_config_drop(cfg)

    state = _build_state(cfg, resolved_data_dir)

    @asynccontextmanager
    async def _lifespan(app: Any):  # type: ignore[no-untyped-def]
        services = _lazy_import("corlinman_server.gateway.services")
        evolution = _lazy_import("corlinman_server.gateway.evolution")
        grpc_mod = _lazy_import("corlinman_server.gateway.grpc")

        cancel = asyncio.Event()
        background: list[asyncio.Task[Any]] = []

        for sibling, name in (
            (services, "services"),
            (evolution, "evolution"),
        ):
            if sibling is None:
                continue
            bootstrap = getattr(sibling, "bootstrap", None)
            if bootstrap is None:
                continue
            try:
                result = bootstrap(state)
                if isinstance(result, Awaitable):
                    await result
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.sibling.bootstrap_failed",
                    sibling=name,
                    error=str(exc),
                )

        if grpc_mod is not None:
            serve = getattr(
                grpc_mod, "serve_placeholder_in_background", None
            )
            if serve is not None:
                try:
                    task = asyncio.create_task(serve(state, cancel))
                    background.append(task)
                except Exception as exc:  # pragma: no cover — sibling-owned
                    logger.warning(
                        "gateway.grpc.bootstrap_failed", error=str(exc)
                    )

        try:
            yield
        finally:
            cancel.set()
            for task in background:
                task.cancel()
            for task in background:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    app = FastAPI(lifespan=_lifespan)
    app.state.corlinman_state = state
    app.state.corlinman_config = cfg
    app.state.corlinman_data_dir = resolved_data_dir

    # Middleware before routes — order matters for ASGI stack walks.
    middleware = _lazy_import("corlinman_server.gateway.middleware")
    if middleware is not None:
        install = getattr(middleware, "install", None)
        if install is not None:
            try:
                install(app, state)
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.middleware.install_failed", error=str(exc)
                )

    # Mount every routes submodule. Each submodule exposes a different
    # composition surface (per the parallel-agent contracts); we wire them
    # individually here to keep entrypoint.py the single composition root.
    _mount_routes(app, state)

    # Degraded-mode safety net: if the routes sibling didn't land yet,
    # mount a single ``/health`` so liveness probes succeed and the
    # operator can see the process is up.
    if "/health" not in {r.path for r in app.routes if hasattr(r, "path")}:

        @app.get("/health")
        async def _health() -> dict[str, str]:  # pragma: no cover — trivial
            return {"status": "ok", "mode": "degraded" if routes is None else "ok"}

    return app


# ---------------------------------------------------------------------------
# CLI / uvicorn driver
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corlinman-gateway",
        description=(
            "Run the corlinman gateway (Python port of the Rust "
            "corlinman-gateway binary)."
        ),
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to the gateway config TOML. Falls back to "
        "$CORLINMAN_CONFIG, then no config (defaults).",
    )
    p.add_argument(
        "--host",
        dest="host",
        default=None,
        help=f"Bind host. Default: $BIND or {DEFAULT_HOST}.",
    )
    p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=None,
        help=f"Bind port. Default: $PORT or {DEFAULT_PORT}.",
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        default=None,
        help="Override the data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level. Default: $LOG_LEVEL or info.",
    )
    return p


def _resolve_bind(cli_host: str | None, cli_port: int | None) -> tuple[str, int]:
    host = cli_host or os.environ.get("BIND") or DEFAULT_HOST
    if cli_port is not None:
        port = cli_port
    else:
        env_port = os.environ.get("PORT")
        port = int(env_port) if env_port and env_port.isdigit() else DEFAULT_PORT
    return host, port


async def _serve(args: argparse.Namespace) -> int:
    """Build the app and run uvicorn until SIGTERM/SIGINT."""
    # Telemetry init (best-effort — missing OTLP endpoint is a no-op
    # inside the helper).
    try:
        from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

        init_telemetry()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("gateway.telemetry.init_failed", error=str(exc))

        def shutdown_telemetry() -> None:  # type: ignore[misc]
            return None

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover — uvicorn is a runtime dep
        raise RuntimeError(
            "uvicorn is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    config_path = _resolve_config_path(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else _resolve_data_dir(None)
    host, port = _resolve_bind(args.host, args.port)

    app = build_app(config_path=config_path, data_dir=data_dir)

    uv_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=args.log_level,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)

    # Wire SIGTERM/SIGINT to uvicorn's graceful-shutdown flag. uvicorn
    # installs its own handlers when run via ``uvicorn.run``; we use
    # ``Server.serve`` so we can return the right exit code.
    loop = asyncio.get_running_loop()
    received: list[str] = []

    def _on_signal(name: str) -> None:
        received.append(name)
        logger.info("gateway.shutdown.signal", signal=name)
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except NotImplementedError:
            # Windows / restricted envs — uvicorn's own signal hooks
            # will still trip; we just won't relay the name. Tests on
            # those platforms are not in scope.
            pass

    logger.info("gateway.serve.start", host=host, port=port)
    await server.serve()
    logger.info("gateway.serve.stopped")

    shutdown_telemetry()
    return SIGTERM_EXIT_CODE if any(r == "SIGTERM" for r in received) else 0


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint.

    Registered (when ``pyproject.toml`` is updated by the integration
    step) as ``corlinman-gateway = "corlinman_server.gateway.lifecycle.entrypoint:main"``.
    """
    args = _build_parser().parse_args(argv)
    try:
        code = asyncio.run(_serve(args))
    except KeyboardInterrupt:
        code = SIGTERM_EXIT_CODE
    sys.exit(code)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "SIGTERM_EXIT_CODE",
    "build_app",
    "main",
]
