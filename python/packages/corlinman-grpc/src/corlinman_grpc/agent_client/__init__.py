"""``corlinman_grpc.agent_client`` — async gRPC client to the Python agent.

1:1 port of the Rust ``corlinman-agent-client`` crate:

* :mod:`client` — connection + bidi ``Agent.Chat`` stream
  (:class:`AgentClient`, :class:`ChatStream`,
  :class:`PlaceholderExecutor`, :func:`resolve_endpoint`,
  :func:`connect_channel`, :func:`inject_trace_context`).
* :mod:`retry` — backoff orchestration
  (:func:`with_retry`, :func:`classify_grpc_error`,
  :func:`next_retry_delay`, :func:`status_to_error`,
  :data:`DEFAULT_SCHEDULE`).
* :mod:`types` — shared types (:class:`FailoverReason`,
  :class:`UpstreamError`, :class:`ConfigError`).

The submodule never imports the wider Python plane (no providers, no
server). Generated stubs come from
``corlinman_grpc._generated.corlinman.v1`` so this package stays the
sole owner of the proto contract.
"""

from __future__ import annotations

from corlinman_grpc.agent_client.client import (
    CHANNEL_CAPACITY,
    DEFAULT_TCP_ADDR,
    AgentClient,
    ChatStream,
    PlaceholderExecutor,
    ToolExecutor,
    connect_channel,
    inject_trace_context,
    resolve_endpoint,
)
from corlinman_grpc.agent_client.retry import (
    DEFAULT_SCHEDULE,
    classify_grpc_error,
    next_retry_delay,
    status_to_error,
    with_retry,
)
from corlinman_grpc.agent_client.types import (
    AgentClientError,
    ConfigError,
    FailoverReason,
    UpstreamError,
)

__all__ = [
    "CHANNEL_CAPACITY",
    "DEFAULT_SCHEDULE",
    "DEFAULT_TCP_ADDR",
    "AgentClient",
    "AgentClientError",
    "ChatStream",
    "ConfigError",
    "FailoverReason",
    "PlaceholderExecutor",
    "ToolExecutor",
    "UpstreamError",
    "classify_grpc_error",
    "connect_channel",
    "inject_trace_context",
    "next_retry_delay",
    "resolve_endpoint",
    "status_to_error",
    "with_retry",
]
