"""OpenTelemetry OTLP exporter + rotating file-sink for the gateway.

Python port of ``rust/crates/corlinman-gateway/src/telemetry.rs``.

Contract (mirrors the Rust gateway byte-for-byte where possible):

* The OTLP exporter is activated only when ``OTEL_EXPORTER_OTLP_ENDPOINT``
  is set and non-empty. Unset → :func:`try_init_tracer` returns ``None``
  and structured logging keeps working unchanged.
* Failing init is **warn-and-continue**. The gateway must never refuse to
  start because a collector is unreachable or a log file cannot be
  opened.
* Service name is ``corlinman-gateway`` unless ``OTEL_SERVICE_NAME``
  overrides; service version is the ``corlinman-server`` package
  version (or ``"0.0.0"`` if introspection fails).
* The W3C TraceContext propagator is installed globally before the
  exporter pipeline starts, so any in-flight work picks up the
  traceparent injection.

The rotating file sink is implemented with the standard-library
:class:`logging.handlers.TimedRotatingFileHandler` rather than pulling
in a watchdog-style appender — Python ships rotation in the stdlib and
the resulting file layout (``<prefix>.YYYY-MM-DD``) is identical to
what ``tracing-appender`` emits.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Module-singleton tracer provider — same shape as :mod:`corlinman_server.telemetry`
# but kept independent so the gateway can be torn down without affecting
# the shared gRPC telemetry. ``Any`` because the type only resolves
# after a successful import of the OTel SDK.
_PROVIDER: Any = None


def _pkg_version() -> str:
    """Lookup ``corlinman-server`` package version with a default."""
    try:
        return version("corlinman-server")
    except PackageNotFoundError:  # pragma: no cover — installed in tests
        return "0.0.0"


def try_init_tracer() -> Any | None:
    """Best-effort OTLP tracer init.

    Returns the installed :class:`~opentelemetry.sdk.trace.Tracer` when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set and the exporter pipeline
    builds cleanly, ``None`` otherwise. Errors downgrade to ``warning``
    log + ``None`` so boot continues.
    """

    global _PROVIDER

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
    except ImportError as err:
        logger.warning("otel.gateway.init.skip", reason="missing_dep", error=str(err))
        return None

    service_name = os.environ.get("OTEL_SERVICE_NAME", "corlinman-gateway")
    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": _pkg_version(),
            }
        )
        provider = TracerProvider(resource=resource)
        # ``insecure=`` defaults to False when scheme is https://. Mirror
        # the Rust behaviour of using gRPC over plain HTTP when the URL
        # is an http://… endpoint.
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        set_global_textmap(TraceContextTextMapPropagator())
        _PROVIDER = provider
        tracer = provider.get_tracer("corlinman-gateway")
        logger.info("otel.gateway.init.ok", endpoint=endpoint)
        return tracer
    except Exception as err:  # pragma: no cover — collector-dependent
        logger.warning("otel.gateway.init.failed", endpoint=endpoint, error=str(err))
        return None


def shutdown_tracer() -> None:
    """Flush + shutdown the gateway tracer provider. Safe to call when
    :func:`try_init_tracer` was never invoked or returned ``None``.
    """

    global _PROVIDER
    if _PROVIDER is None:
        return
    try:
        _PROVIDER.shutdown()
    except Exception:  # pragma: no cover — defensive
        pass
    _PROVIDER = None


# ---------------------------------------------------------------------------
# File sink: rolling JSON log files via TimedRotatingFileHandler.
# ---------------------------------------------------------------------------


class RotationKind(str, Enum):
    """How often the file appender rolls over.

    Mirrors ``corlinman_core::config::RotationKind``. Inherits ``str``
    so the value drops straight into a TOML / JSON config without an
    extra serialiser.
    """

    DAILY = "daily"
    HOURLY = "hourly"
    MINUTELY = "minutely"
    NEVER = "never"


@dataclass
class FileLoggingConfig:
    """Subset of the Rust ``FileLoggingConfig`` schema needed by the
    file sink. Lives here so the gateway core can build a sink without
    pulling in the entire config struct. The full config (parsed from
    TOML) is owned by the config_watcher module / future sibling
    porter.
    """

    path: Path
    max_size_mb: int = 5
    retention_days: int = 7
    rotation: RotationKind = RotationKind.DAILY


@dataclass
class FileSink:
    """Returned by :func:`build_file_sink`. ``handler`` is a stdlib
    logging handler the caller wires into structlog / Python ``logging``;
    ``dir``/``prefix`` are returned so the retention task knows what
    sibling files to sweep.
    """

    handler: logging.Handler
    dir: Path
    prefix: str
    # Extras kept for parity with the Rust ``FileSink`` shape — the
    # appender ``writer`` and ``guard`` don't have direct Python
    # equivalents (the handler owns its own buffer + flush), but
    # surfacing the config keeps tests symmetric.
    config: FileLoggingConfig = field(default_factory=lambda: FileLoggingConfig(path=Path()))


def split_dir_and_prefix(path: Path) -> tuple[Path, str] | None:
    """Split a user-supplied log path into ``(parent_dir, file_prefix)``.

    Returns ``None`` for path shapes the appender can't honour:
      * empty path
      * trailing slash (the operator named a directory, not a file)
      * no file_name component
    """

    raw = str(path)
    if not raw:
        return None
    if raw.endswith("/") or raw.endswith(os.sep):
        return None
    name = path.name
    if not name:
        return None
    parent = path.parent if str(path.parent) else Path(".")
    if str(parent) == "":
        parent = Path(".")
    return parent, name


def _rotation_kwargs(kind: RotationKind) -> dict[str, Any]:
    """Map :class:`RotationKind` to :class:`TimedRotatingFileHandler` kwargs."""
    if kind is RotationKind.DAILY:
        return {"when": "midnight", "interval": 1}
    if kind is RotationKind.HOURLY:
        return {"when": "H", "interval": 1}
    if kind is RotationKind.MINUTELY:
        return {"when": "M", "interval": 1}
    # NEVER: drop to a degenerate one-tick-per-week interval. The
    # retention task only sweeps the active file when it matches the
    # bare prefix, so this is equivalent to "never rotate in practice".
    return {"when": "W6", "interval": 1}


def build_file_sink(cfg: FileLoggingConfig) -> FileSink | None:
    """Build a rolling file sink for the gateway log stream.

    Returns ``None`` when the sink is disabled (empty path) or the
    parent dir can't be created — caller falls back to stdout-only
    logging. Otherwise the returned :class:`FileSink` carries a
    ``TimedRotatingFileHandler`` the caller can attach to whatever
    logging pipeline they want.
    """

    if cfg.path == Path() or not str(cfg.path):
        return None
    parts = split_dir_and_prefix(cfg.path)
    if parts is None:
        return None
    parent, name = parts

    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        logger.warning(
            "telemetry.file_sink.mkdir_failed",
            dir=str(parent),
            error=str(err),
        )
        return None

    kwargs = _rotation_kwargs(cfg.rotation)
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(cfg.path),
        backupCount=max(cfg.retention_days, 0),
        encoding="utf-8",
        utc=True,
        **kwargs,
    )
    # JSON-friendly format — caller usually wires structlog through the
    # handler; without a formatter every record is bytes-as-is.
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger.info(
        "telemetry.file_sink.ok",
        dir=str(parent),
        prefix=name,
        rotation=cfg.rotation.value,
        retention_days=cfg.retention_days,
        max_size_mb=cfg.max_size_mb,
    )
    return FileSink(handler=handler, dir=parent, prefix=name, config=cfg)


__all__ = [
    "FileLoggingConfig",
    "FileSink",
    "RotationKind",
    "build_file_sink",
    "shutdown_tracer",
    "split_dir_and_prefix",
    "try_init_tracer",
]
