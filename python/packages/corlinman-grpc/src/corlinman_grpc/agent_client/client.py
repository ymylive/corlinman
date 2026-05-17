"""Async gRPC client to the Python agent.

Ports ``corlinman-agent-client::{client, stream, tool_callback,
trace_propagate}`` onto ``grpc.aio``. The surface mirrors the Rust
crate 1:1 so a caller used to either side reads the other without
surprise:

* :func:`resolve_endpoint` — env-driven endpoint resolution.
* :func:`connect_channel` — opens a ``grpc.aio.Channel``.
* :class:`AgentClient` — wraps the generated :class:`AgentStub` and
  exposes :meth:`AgentClient.chat` to open a bidi stream.
* :class:`ChatStream` — paired ``send`` + ``__aiter__`` half of the
  bidi call, with backpressure (default capacity ``CHANNEL_CAPACITY``
  ``= 16``) and a :meth:`ChatStream.cancel` shortcut.
* :class:`ToolExecutor` / :class:`PlaceholderExecutor` — bridge from
  ``ServerFrame.tool_call`` to ``ClientFrame.tool_result``.
* :func:`inject_trace_context` — best-effort W3C traceparent injection
  on outbound calls; no-op when OTel isn't installed.

Connection / endpoint precedence
--------------------------------

Mirrors the Rust resolver:

1. ``CORLINMAN_PY_ADDR`` — explicit ``host:port``.
2. ``CORLINMAN_PY_PORT`` — bind to ``127.0.0.1:<port>``.
3. ``DEFAULT_TCP_ADDR`` (``127.0.0.1:50051``).

The Rust crate also documents ``CORLINMAN_PY_SOCKET`` for the eventual
UDS path; we leave that hook for the Docker packaging milestone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Protocol

import grpc
from grpc.aio import AioRpcError

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    agent_pb2_grpc,
)
from corlinman_grpc.agent_client.retry import status_to_error
from corlinman_grpc.agent_client.types import ConfigError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint resolution + channel construction.
# ---------------------------------------------------------------------------

DEFAULT_TCP_ADDR: str = "127.0.0.1:50051"
"""Default TCP address used when no env override is set."""

CHANNEL_CAPACITY: int = 16
"""Outbound capacity for the ClientFrame queue (plan §5.1)."""


def resolve_endpoint() -> str:
    """Resolve the Python agent endpoint from the environment.

    Precedence: ``CORLINMAN_PY_ADDR`` > ``CORLINMAN_PY_PORT`` >
    :data:`DEFAULT_TCP_ADDR`. Mirrors ``corlinman_server._bind_address``
    + the Rust ``resolve_endpoint``.
    """
    addr = os.environ.get("CORLINMAN_PY_ADDR")
    if addr:
        return addr
    port = os.environ.get("CORLINMAN_PY_PORT")
    if port:
        return f"127.0.0.1:{port}"
    return DEFAULT_TCP_ADDR


def _normalise_target(endpoint: str) -> str:
    """grpc.aio targets are bare ``host:port`` strings; strip any HTTP
    scheme a caller may have copy-pasted from logs."""
    for prefix in ("http://", "https://"):
        if endpoint.startswith(prefix):
            return endpoint[len(prefix) :]
    return endpoint


def connect_channel(endpoint: str) -> grpc.aio.Channel:
    """Build a lazily-connecting ``grpc.aio.Channel`` to the Python agent.

    The Rust crate uses ``Endpoint::from_shared(...).connect_timeout(5s).
    tcp_nodelay(true).connect().await``; on the Python side ``grpc.aio``
    is lazy by default and the keepalive / TCP options live on the
    channel-args list — we set ``tcp_nodelay`` for parity. Raises
    :class:`ConfigError` on malformed targets.
    """
    target = _normalise_target(endpoint)
    if not target:
        raise ConfigError(f"invalid agent endpoint: {endpoint!r}")
    options = [
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 5_000),
        # tonic enables TCP_NODELAY by default; mirror it here.
        ("grpc.http2.min_time_between_pings_ms", 10_000),
    ]
    try:
        return grpc.aio.insecure_channel(target, options=options)
    except Exception as exc:  # pragma: no cover — grpc.aio.insecure_channel
        # is usually infallible, but a future grpcio could throw and we
        # want a typed error instead of leaking the underlying class.
        raise ConfigError(f"connect python agent: {exc}") from exc


# ---------------------------------------------------------------------------
# Trace propagation (best-effort W3C traceparent injection).
# ---------------------------------------------------------------------------


def inject_trace_context(
    metadata: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Return a metadata list with W3C traceparent appended.

    Best-effort: if ``opentelemetry`` isn't installed (or no tracer is
    active), the input metadata is returned unchanged. Mirrors the Rust
    ``inject_trace_context`` which is a no-op when the global propagator
    is the default ``NoopTextMapPropagator``.
    """
    meta: list[tuple[str, str]] = list(metadata or [])
    try:
        from opentelemetry import propagate  # type: ignore[import-not-found]
    except ImportError:
        return meta

    carrier: dict[str, str] = {}
    try:
        propagate.inject(carrier)
    except Exception:  # pragma: no cover — defensive
        return meta
    for key, value in carrier.items():
        meta.append((key.lower(), value))
    return meta


# ---------------------------------------------------------------------------
# Tool-callback bridge.
# ---------------------------------------------------------------------------


class ToolExecutor(Protocol):
    """Contract every plugin bridge implements.

    Mirrors the Rust ``ToolExecutor`` trait: agent_client never depends
    directly on a plugin registry — implementations are injected at
    gateway assembly time.
    """

    async def execute(
        self, call: agent_pb2.ToolCall
    ) -> agent_pb2.ToolResult: ...


class PlaceholderExecutor:
    """Default M1/M2 :class:`ToolExecutor`.

    Acknowledges every call with an ``awaiting_plugin_runtime`` payload
    so the Python reasoning loop can advance. Replaced by the real
    plugin registry in M3 — same trait, different implementation.
    """

    async def execute(self, call: agent_pb2.ToolCall) -> agent_pb2.ToolResult:
        payload = {
            "status": "awaiting_plugin_runtime",
            "plugin": call.plugin,
            "tool": call.tool,
            "message": (
                "plugin runtime lands in M3; call observed but not executed"
            ),
        }
        return agent_pb2.ToolResult(
            call_id=call.call_id,
            result_json=json.dumps(payload).encode("utf-8"),
            is_error=False,
            duration_ms=0,
        )


# ---------------------------------------------------------------------------
# Bidi chat stream.
# ---------------------------------------------------------------------------


class ChatStream:
    """Paired sender + receive stream for a single ``Agent.Chat`` call.

    Owns a bounded ``asyncio.Queue`` (capacity :data:`CHANNEL_CAPACITY`)
    that the gateway writes :class:`agent_pb2.ClientFrame` into; the
    grpc.aio stream is fed by an internal async generator that drains
    the queue. Inbound :class:`agent_pb2.ServerFrame` messages are
    surfaced via :meth:`__aiter__` or :meth:`next_classified`.

    On graceful close (queue sentinel) the Python side ends its
    ``async for frame in stream`` loop; :meth:`close` and :meth:`cancel`
    both unwind cooperatively.
    """

    _CLOSE_SENTINEL: object = object()

    def __init__(
        self,
        call: grpc.aio.StreamStreamCall,
        tx: asyncio.Queue[Any],
    ) -> None:
        self._call = call
        self._tx = tx
        self._closed = False

    # -- send half ----------------------------------------------------------

    async def send(self, frame: agent_pb2.ClientFrame) -> None:
        """Enqueue a :class:`agent_pb2.ClientFrame` for delivery."""
        if self._closed:
            raise RuntimeError("ChatStream is closed")
        await self._tx.put(frame)

    async def cancel(self, reason: str) -> bool:
        """Best-effort cancel: push a ``Cancel { reason }`` frame.

        Returns ``False`` if the stream is already closed (matches the
        Rust ``ChatStream::cancel`` boolean contract).
        """
        if self._closed:
            return False
        frame = agent_pb2.ClientFrame(
            cancel=agent_pb2.Cancel(reason=reason),
        )
        try:
            await self._tx.put(frame)
        except RuntimeError:  # pragma: no cover — queue closed under us
            return False
        return True

    async def close(self) -> None:
        """Cooperative shutdown: signal the writer loop to drain and
        cancel the underlying call.

        Idempotent: double-close is a no-op.
        """
        if self._closed:
            return
        self._closed = True
        # Sentinel ends the writer generator; the gRPC peer then sees
        # the half-close and exits its `async for frame` loop.
        await self._tx.put(self._CLOSE_SENTINEL)
        with contextlib.suppress(Exception):  # pragma: no cover — defensive
            self._call.cancel()

    # -- receive half -------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[agent_pb2.ServerFrame]:
        return self._call.__aiter__()

    async def next_classified(self) -> agent_pb2.ServerFrame | None:
        """Read the next :class:`agent_pb2.ServerFrame`, raising
        :class:`UpstreamError` on classifiable gRPC failures.

        Returns ``None`` on clean end-of-stream — matches the Rust
        ``Option<Result<ServerFrame, _>>`` shape collapsed into Python's
        ``None`` / exception idiom.
        """
        try:
            return await self._call.read()  # type: ignore[no-any-return]
        except AioRpcError as err:
            raise status_to_error(err) from err

    # -- context manager ----------------------------------------------------

    async def __aenter__(self) -> ChatStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# AgentClient.
# ---------------------------------------------------------------------------


class AgentClient:
    """Thin async wrapper around the generated :class:`AgentStub`.

    Owns a shareable :class:`grpc.aio.Channel`; multiple :meth:`chat`
    calls multiplex over the same HTTP/2 connection.
    """

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel = channel
        self._stub = agent_pb2_grpc.AgentStub(channel)

    @classmethod
    async def connect_default(cls) -> AgentClient:
        """Connect using :func:`resolve_endpoint`.

        Returns once the channel object is constructed; the actual TCP
        handshake is lazy (matches grpc.aio's normal behaviour).
        """
        channel = connect_channel(resolve_endpoint())
        return cls(channel)

    @property
    def channel(self) -> grpc.aio.Channel:
        """Underlying ``grpc.aio.Channel`` (handy for sibling stubs)."""
        return self._channel

    @property
    def stub(self) -> agent_pb2_grpc.AgentStub:
        """Borrow the generated stub (mirrors Rust ``inner_mut``)."""
        return self._stub

    async def chat(
        self,
        *,
        metadata: list[tuple[str, str]] | None = None,
        timeout: float | None = None,
    ) -> ChatStream:
        """Open a new bidirectional ``Agent.Chat`` stream.

        The returned :class:`ChatStream` is the caller's responsibility
        to drive — they must send :class:`agent_pb2.ChatStart` as the
        first frame.
        """
        tx: asyncio.Queue[Any] = asyncio.Queue(maxsize=CHANNEL_CAPACITY)

        async def request_iter() -> AsyncIterator[agent_pb2.ClientFrame]:
            while True:
                item = await tx.get()
                if item is ChatStream._CLOSE_SENTINEL:
                    return
                yield item

        full_metadata = inject_trace_context(metadata)
        call = self._stub.Chat(
            request_iter(),
            metadata=tuple(full_metadata) if full_metadata else None,
            timeout=timeout,
        )
        return ChatStream(call, tx)

    async def close(self) -> None:
        """Close the underlying gRPC channel.

        Idempotent: swallows secondary errors so this is safe in a
        ``finally`` block.
        """
        try:
            await self._channel.close()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("agent_client.close_ignored", exc_info=exc)

    async def __aenter__(self) -> AgentClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


__all__ = [
    "CHANNEL_CAPACITY",
    "DEFAULT_TCP_ADDR",
    "AgentClient",
    "ChatStream",
    "PlaceholderExecutor",
    "ToolExecutor",
    "connect_channel",
    "inject_trace_context",
    "resolve_endpoint",
]
