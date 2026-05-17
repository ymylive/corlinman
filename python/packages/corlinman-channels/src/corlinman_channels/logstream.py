"""LogStream WebSocket adapter.

Port of ``rust/.../logstream.rs`` (which in the Rust crate is a stub plus
TODOs describing the planned structured-JSON frame shape — plan §12
``websocket-compat``). On the Python plane we ship the working subscriber
shape directly so the gateway and any UI consumer can plug into the same
:class:`InboundAdapter` contract the QQ / Telegram adapters use.

## Frame format

Each WebSocket text frame is one structured JSON object::

    {
      "channel": "logstream",         # optional, defaults to "logstream"
      "stream":  "agent.brain",       # logical sub-stream / topic
      "level":   "info",
      "message": "started turn",
      "ts":      1700000000,
      "fields":  { "user_id": "abc" }
    }

Unknown / extra keys round-trip via the :class:`LogFrame.fields` mapping
so a forward-compatible reader doesn't drop information.

Binary frames are ignored (matching the OneBot adapter's policy); malformed
JSON frames are skipped with no fatal error so a single broken publisher
doesn't kill the subscription.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from corlinman_channels.common import (
    ChannelBinding,
    ConfigError,
    InboundEvent,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Reconnect schedule mirrors the OneBot one; log streaming shares the same
#: "transient drops are expected" model.
RECONNECT_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

#: WebSocket-level ping interval (seconds). Logs streams idle for long
#: periods; a 60s ping keeps NAT mappings alive without flooding.
PING_INTERVAL: float = 60.0


# ===========================================================================
# Wire types
# ===========================================================================


@dataclass(slots=True)
class LogFrame:
    """One decoded log frame.

    ``stream`` is the logical sub-channel (think Kafka topic), ``level``
    is the syslog-style severity, and ``message`` is the human-readable
    body. Producer-side fields beyond these five round-trip via
    :attr:`fields`.
    """

    stream: str = ""
    level: str = "info"
    message: str = ""
    ts: int = 0
    fields: dict[str, Any] = field(default_factory=dict)


def parse_frame(raw: dict[str, Any]) -> LogFrame:
    """Decode one log-frame dict into a :class:`LogFrame`.

    Forward-compatible — unknown keys land in :attr:`LogFrame.fields`
    rather than failing the decode.
    """
    known = {"channel", "stream", "level", "message", "ts", "fields"}
    extra_fields: dict[str, Any] = {}
    base_fields = raw.get("fields")
    if isinstance(base_fields, dict):
        extra_fields.update(base_fields)
    for k, v in raw.items():
        if k not in known:
            extra_fields[k] = v
    return LogFrame(
        stream=str(raw.get("stream", "")),
        level=str(raw.get("level", "info")),
        message=str(raw.get("message", "")),
        ts=int(raw.get("ts", 0)) if isinstance(raw.get("ts"), (int, float, str)) and _is_int_like(raw.get("ts")) else 0,
        fields=extra_fields,
    )


def _is_int_like(v: Any) -> bool:
    """Predicate for safe int(...) coercion in :func:`parse_frame`."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return v.is_integer()
    if isinstance(v, str):
        try:
            int(v)
            return True
        except ValueError:
            return False
    return False


# ===========================================================================
# Config + adapter
# ===========================================================================


@dataclass(slots=True)
class LogStreamConfig:
    """Configuration for :class:`LogStreamAdapter`.

    ``url`` is the full WebSocket URL (``ws://host:port/logs``).
    ``access_token`` becomes ``Authorization: Bearer <token>`` when set.
    ``account`` is the optional logical account id surfaced on the
    normalized :class:`ChannelBinding` — log streams are inherently
    multi-tenant so callers usually supply their tenant slug here.
    """

    url: str
    access_token: str | None = None
    account: str = "default"
    reconnect_schedule: tuple[float, ...] = RECONNECT_SCHEDULE
    ping_interval: float = PING_INTERVAL


class LogStreamAdapter:
    """WebSocket subscriber for the log stream.

    Same shape as :class:`corlinman_channels.onebot.OneBotAdapter`:
    ``async with adapter:`` connects, ``async for event in
    adapter.inbound():`` yields normalized :class:`InboundEvent` objects.

    The reader loop reconnects on transient transport failures; permanent
    config errors raise :class:`ConfigError` at construction time so the
    caller fails fast.
    """

    def __init__(self, config: LogStreamConfig) -> None:
        if not config.url:
            raise ConfigError("LogStreamConfig.url is empty")
        self._cfg = config
        self._closed = False
        self._inbound_q: asyncio.Queue[LogFrame] = asyncio.Queue(maxsize=256)
        self._reader_task: asyncio.Task[None] | None = None
        self._ws: ClientConnection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> LogStreamAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the background reader loop. See :meth:`OneBotAdapter.connect`."""
        if self._reader_task is not None:
            return
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="logstream-reader"
        )

    async def close(self) -> None:
        """Shut down the reader loop and the underlying WS."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[LogFrame]]:
        """Yield one :class:`InboundEvent` per inbound :class:`LogFrame`.

        The normalized envelope's ``text`` is the frame's ``message``,
        ``binding.thread`` is the ``stream`` slug, and ``payload`` carries
        the full :class:`LogFrame` so consumers can read ``level`` /
        structured ``fields``.
        """
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                frame = await self._inbound_q.get()
            except asyncio.CancelledError:
                return
            yield _normalize_log_frame(frame, self._cfg.account)

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Connect → consume → on disconnect, sleep + retry."""
        attempt = 0
        schedule = self._cfg.reconnect_schedule or RECONNECT_SCHEDULE
        while not self._closed:
            try:
                await self._connect_once()
                if self._closed:
                    return
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._closed:
                return
            delay = schedule[min(attempt, len(schedule) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _connect_once(self) -> None:
        headers: list[tuple[str, str]] = []
        if self._cfg.access_token:
            headers.append(("Authorization", f"Bearer {self._cfg.access_token}"))
        async with websockets.connect(
            self._cfg.url,
            additional_headers=headers or None,
            ping_interval=self._cfg.ping_interval,
        ) as ws:
            self._ws = ws
            try:
                async for raw_msg in ws:
                    if self._closed:
                        break
                    if isinstance(raw_msg, bytes):
                        continue
                    try:
                        raw = json.loads(raw_msg)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(raw, dict):
                        continue
                    frame = parse_frame(raw)
                    await self._inbound_q.put(frame)
            finally:
                self._ws = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_log_frame(frame: LogFrame, account: str) -> InboundEvent[LogFrame]:
    """Convert a :class:`LogFrame` into the normalized envelope.

    The ``ChannelBinding`` shape used here treats the stream slug as both
    the conversation thread and the sender — log streams have no concept
    of a separate sender, and using the same value for both keeps
    ``session_key()`` stable per-stream.
    """
    stream = frame.stream or "default"
    binding = ChannelBinding(
        channel="logstream",
        account=account,
        thread=stream,
        sender=stream,
    )
    return InboundEvent(
        channel="logstream",
        binding=binding,
        text=frame.message,
        message_id=None,
        timestamp=frame.ts,
        mentioned=False,
        attachments=[],
        payload=frame,
    )


__all__ = [
    "PING_INTERVAL",
    "RECONNECT_SCHEDULE",
    "LogFrame",
    "LogStreamAdapter",
    "LogStreamConfig",
    "parse_frame",
]
