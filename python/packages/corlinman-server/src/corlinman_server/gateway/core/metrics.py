"""Prometheus metrics facade for the Python gateway.

Python port of ``rust/crates/corlinman-gateway/src/metrics.rs`` (which is
itself a re-export shell over ``corlinman-core::metrics``). The names and
labels match the Rust facade byte-for-byte so a single Grafana / alerting
config covers both runtimes.

The metric handles live on a dedicated :class:`~prometheus_client.CollectorRegistry`
called :data:`REGISTRY`. We do not use the prometheus_client default
registry so that the Python ``Process`` collector (or anything else that
auto-registers globally) doesn't accidentally show up in our /metrics
output. Callers wanting that should mount a separate scrape endpoint.

| Metric                                     | Type      | Labels              |
|--------------------------------------------|-----------|---------------------|
| ``corlinman_http_requests_total``          | Counter   | ``route``, ``status`` |
| ``corlinman_chat_stream_duration_seconds`` | Histogram | ``model``, ``finish`` |
| ``corlinman_plugin_execute_total``         | Counter   | ``plugin``, ``status``|
| ``corlinman_plugin_execute_duration_seconds`` | Histogram | ``plugin`` |
| ``corlinman_backoff_retries_total``        | Counter   | ``reason`` |
| ``corlinman_agent_grpc_inflight``          | Gauge     | — |
| ``corlinman_channels_rate_limited_total``  | Counter   | ``channel``, ``reason`` |
| ``corlinman_vector_query_duration_seconds``| Histogram | ``stage`` |
| ``corlinman_approvals_total``              | Counter   | ``decision`` |
| ``corlinman_log_files_removed_total``      | Counter   | ``reason`` |

A best-effort :func:`init` pre-touches every label set with a sentinel
``startup`` value so the names appear in ``/metrics`` from the first
scrape even if no traffic has flowed yet.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Dedicated registry so we don't fight the global default registry.
REGISTRY: CollectorRegistry = CollectorRegistry(auto_describe=True)

# ---- HTTP layer -------------------------------------------------------------
HTTP_REQUESTS: Counter = Counter(
    "corlinman_http_requests_total",
    "HTTP requests served, labelled by route + status code",
    labelnames=("route", "status"),
    registry=REGISTRY,
)

# ---- Chat hot path ----------------------------------------------------------
CHAT_STREAM_DURATION: Histogram = Histogram(
    "corlinman_chat_stream_duration_seconds",
    "End-to-end chat stream duration in seconds",
    labelnames=("model", "finish"),
    registry=REGISTRY,
)

# ---- Plugin runtime ---------------------------------------------------------
PLUGIN_EXECUTE_TOTAL: Counter = Counter(
    "corlinman_plugin_execute_total",
    "Plugin tool invocations, labelled by plugin name + outcome",
    labelnames=("plugin", "status"),
    registry=REGISTRY,
)
PLUGIN_EXECUTE_DURATION: Histogram = Histogram(
    "corlinman_plugin_execute_duration_seconds",
    "Plugin tool invocation duration in seconds",
    labelnames=("plugin",),
    registry=REGISTRY,
)

# ---- Backoff / rate limiting -----------------------------------------------
BACKOFF_RETRIES: Counter = Counter(
    "corlinman_backoff_retries_total",
    "Number of retries the backoff helper has scheduled, by reason",
    labelnames=("reason",),
    registry=REGISTRY,
)
CHANNELS_RATE_LIMITED: Counter = Counter(
    "corlinman_channels_rate_limited_total",
    "Inbound messages dropped by channel rate-limit, by (channel, reason)",
    labelnames=("channel", "reason"),
    registry=REGISTRY,
)

# ---- gRPC inflight / vector store ------------------------------------------
AGENT_GRPC_INFLIGHT: Gauge = Gauge(
    "corlinman_agent_grpc_inflight",
    "Number of in-flight gRPC calls to the agent server",
    registry=REGISTRY,
)
VECTOR_QUERY_DURATION: Histogram = Histogram(
    "corlinman_vector_query_duration_seconds",
    "Per-stage duration of a vector store query",
    labelnames=("stage",),
    registry=REGISTRY,
)

# ---- Approvals / log retention --------------------------------------------
APPROVALS_TOTAL: Counter = Counter(
    "corlinman_approvals_total",
    "Tool approval decisions, labelled by terminal decision",
    labelnames=("decision",),
    registry=REGISTRY,
)
LOG_FILES_REMOVED: Counter = Counter(
    "corlinman_log_files_removed_total",
    "Rotated log files unlinked by the retention task, by reason",
    labelnames=("reason",),
    registry=REGISTRY,
)


def init() -> None:
    """Pre-touch every label set with a sentinel ``startup`` value so
    each metric family shows up in ``/metrics`` from the very first
    scrape. Idempotent — re-incrementing a counter by 0 is a no-op.
    """

    HTTP_REQUESTS.labels(route="startup", status="0").inc(0)
    PLUGIN_EXECUTE_TOTAL.labels(plugin="startup", status="ok").inc(0)
    BACKOFF_RETRIES.labels(reason="startup").inc(0)
    CHANNELS_RATE_LIMITED.labels(channel="startup", reason="startup").inc(0)
    CHAT_STREAM_DURATION.labels(model="startup", finish="startup").observe(0.0)
    PLUGIN_EXECUTE_DURATION.labels(plugin="startup").observe(0.0)
    VECTOR_QUERY_DURATION.labels(stage="startup").observe(0.0)
    APPROVALS_TOTAL.labels(decision="startup").inc(0)
    LOG_FILES_REMOVED.labels(reason="startup").inc(0)
    # AGENT_GRPC_INFLIGHT is unlabelled — touching .set(0) is enough.
    AGENT_GRPC_INFLIGHT.set(0)


def encode() -> bytes:
    """Render the current registry to the Prometheus text exposition
    format. Mirrors the Rust ``encode()`` shape used by ``/metrics``."""

    return generate_latest(REGISTRY)


__all__ = [
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
    "init",
]
