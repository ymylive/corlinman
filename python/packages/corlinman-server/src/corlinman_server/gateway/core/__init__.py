"""``corlinman_server.gateway.core`` — runtime primitives ported from
``rust/crates/corlinman-gateway/src``.

Submodules:
    * :mod:`state` — :class:`AppState` dataclass + FastAPI dependency.
    * :mod:`server` — FastAPI app factory + uvicorn boot helpers.
    * :mod:`telemetry` — OTLP exporter init + structlog binding (gateway
      flavour; sits alongside the existing :mod:`corlinman_server.telemetry`
      module which is shared across gRPC + HTTP planes).
    * :mod:`metrics` — Prometheus metric registry + handles (same names as
      the Rust gateway).
    * :mod:`log_broadcast` — in-process log-event fan-out via
      :class:`asyncio.Queue` + a websocket / SSE-friendly subscriber API.
    * :mod:`log_retention` — periodic cleanup task for rotated log files.
    * :mod:`config_watcher` — watchdog-based hot-reload of the gateway TOML.
    * :mod:`shutdown` — SIGTERM / SIGINT graceful-shutdown helpers.
"""

from __future__ import annotations

from corlinman_server.gateway.core.config_watcher import (
    DEFAULT_DEBOUNCE_SECONDS,
    ConfigWatcher,
    ReloadReport,
    diff_sections,
)
from corlinman_server.gateway.core.log_broadcast import (
    DEFAULT_CAPACITY,
    LogBroadcaster,
    LogRecord,
    LogSubscriber,
    make_structlog_processor,
)
from corlinman_server.gateway.core.log_retention import (
    SWEEP_INTERVAL_SECONDS,
    LogRetentionTask,
    sweep_once,
)
from corlinman_server.gateway.core.metrics import (
    AGENT_GRPC_INFLIGHT,
    APPROVALS_TOTAL,
    BACKOFF_RETRIES,
    CHANNELS_RATE_LIMITED,
    CHAT_STREAM_DURATION,
    HTTP_REQUESTS,
    LOG_FILES_REMOVED,
    PLUGIN_EXECUTE_DURATION,
    PLUGIN_EXECUTE_TOTAL,
    REGISTRY,
    VECTOR_QUERY_DURATION,
    encode,
    init as init_metrics,
)
from corlinman_server.gateway.core.server import (
    GatewayServer,
    build_app,
    run_uvicorn,
)
from corlinman_server.gateway.core.shutdown import (
    EXIT_CODE_ON_SIGNAL,
    ShutdownReason,
    install_signal_handlers,
    wait_for_signal,
)
from corlinman_server.gateway.core.state import AppState, get_app_state
from corlinman_server.gateway.core.telemetry import (
    FileLoggingConfig,
    FileSink,
    RotationKind,
    build_file_sink,
    shutdown_tracer,
    try_init_tracer,
)

__all__ = [
    # config_watcher
    "ConfigWatcher",
    "DEFAULT_DEBOUNCE_SECONDS",
    "ReloadReport",
    "diff_sections",
    # log_broadcast
    "DEFAULT_CAPACITY",
    "LogBroadcaster",
    "LogRecord",
    "LogSubscriber",
    "make_structlog_processor",
    # log_retention
    "LogRetentionTask",
    "SWEEP_INTERVAL_SECONDS",
    "sweep_once",
    # metrics
    "AGENT_GRPC_INFLIGHT",
    "APPROVALS_TOTAL",
    "BACKOFF_RETRIES",
    "CHANNELS_RATE_LIMITED",
    "CHAT_STREAM_DURATION",
    "HTTP_REQUESTS",
    "LOG_FILES_REMOVED",
    "PLUGIN_EXECUTE_DURATION",
    "PLUGIN_EXECUTE_TOTAL",
    "REGISTRY",
    "VECTOR_QUERY_DURATION",
    "encode",
    "init_metrics",
    # server
    "GatewayServer",
    "build_app",
    "run_uvicorn",
    # shutdown
    "EXIT_CODE_ON_SIGNAL",
    "ShutdownReason",
    "install_signal_handlers",
    "wait_for_signal",
    # state
    "AppState",
    "get_app_state",
    # telemetry
    "FileLoggingConfig",
    "FileSink",
    "RotationKind",
    "build_file_sink",
    "shutdown_tracer",
    "try_init_tracer",
]
