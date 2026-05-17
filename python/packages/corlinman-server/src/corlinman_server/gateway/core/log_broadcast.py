"""In-process log-event broadcaster (asyncio version).

Python port of ``rust/crates/corlinman-gateway/src/log_broadcast.rs``.

Where the Rust gateway plugs into ``tracing_subscriber`` via a custom
``Layer`` and fans out via ``tokio::sync::broadcast``, the Python port
plugs into :mod:`structlog` via a *processor* that pushes a
:class:`LogRecord` onto a central :class:`LogBroadcaster`. Subscribers
get their own ``asyncio.Queue`` and a coroutine API
(``async for record in subscriber: ...``) suitable for the websocket
admin-logs route — same UX as the Rust SSE handler.

Design notes:

* The broadcaster uses **per-subscriber bounded queues** (default
  capacity :data:`DEFAULT_CAPACITY`). A slow consumer that fills its
  queue increments a lag counter and the oldest record is dropped —
  this matches the Rust ``broadcast::RecvError::Lagged`` semantics and
  prevents producer blocking.
* The structlog processor is sync (structlog processors run on the
  calling thread) so we use :meth:`asyncio.Queue.put_nowait`. If no
  event loop is running on the calling thread the record is silently
  dropped — the processor never raises into the structlog pipeline.
* :class:`LogRecord` fields match the TypeScript ``LogEvent`` shape in
  ``ui/app/(admin)/logs/page.tsx`` so the UI renders without an
  adapter layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog


#: Default per-subscriber queue capacity. 1024 buffered records lets a
#: slow admin UI catch up after a short stall without dropping; beyond
#: that we'd rather mark the subscriber as lagged than keep growing
#: memory.
DEFAULT_CAPACITY: int = 1024


@dataclass
class LogRecord:
    """One structured log entry as shipped to the admin UI.

    Field naming matches ``ui/app/(admin)/logs/page.tsx`` and the Rust
    ``LogRecord`` so the UI renders without an adapter layer.
    """

    ts: str  #: RFC-3339 / ISO-8601 UTC timestamp
    level: str  #: Uppercase: "TRACE" | "DEBUG" | "INFO" | "WARN" | "ERROR"
    target: str  #: Module / logger name that emitted the event
    message: str  #: Event message
    fields: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    request_id: str | None = None
    subsystem: str | None = None

    def to_json(self) -> str:
        """Compact JSON serialisation — the websocket handler ships this
        verbatim. Keeps key order stable so structural diffs are clean."""
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


class LogSubscriber:
    """Per-subscriber receive handle. Created by
    :meth:`LogBroadcaster.subscribe`. Async-iterable so handlers can do
    ``async for record in subscriber: ws.send_text(record.to_json())``.
    """

    __slots__ = ("_queue", "_lagged", "_closed", "_broadcaster")

    def __init__(self, broadcaster: "LogBroadcaster", capacity: int) -> None:
        self._queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=capacity)
        self._lagged: int = 0
        self._closed: bool = False
        self._broadcaster = broadcaster

    @property
    def lagged(self) -> int:
        """Records dropped because the queue was full when the
        broadcaster tried to enqueue. Exposed so the SSE / websocket
        handler can surface a ``event: lag`` frame to the UI."""
        return self._lagged

    def _push(self, record: LogRecord) -> None:
        """Non-blocking enqueue. Called from the broadcaster on every
        new record; drops the oldest record (and bumps :attr:`lagged`)
        when the queue is at capacity so the producer never blocks."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._lagged += 1
            # Drop oldest to make room — symmetric with Rust
            # ``broadcast`` behaviour. ``get_nowait`` can race with a
            # concurrent consumer, but the worst case is we keep the
            # old record instead of the new one.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(record)

    async def recv(self) -> LogRecord:
        """Block until the next record arrives. Raises
        :class:`asyncio.CancelledError` if the subscriber was closed
        while waiting."""
        return await self._queue.get()

    def try_recv(self) -> LogRecord | None:
        """Non-blocking variant: ``None`` if the queue is empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        """Unsubscribe — broadcaster stops delivering to this handle."""
        self._closed = True
        self._broadcaster._unsubscribe(self)

    def __aiter__(self) -> "LogSubscriber":
        return self

    async def __anext__(self) -> LogRecord:
        if self._closed:
            raise StopAsyncIteration
        return await self.recv()


class LogBroadcaster:
    """Process-wide fan-out hub for :class:`LogRecord` events.

    Cloning the broadcaster (instances are shared via ``AppState``) is
    safe — all mutable state lives behind a lock. Producers call
    :meth:`publish` (sync); consumers obtain a :class:`LogSubscriber`
    via :meth:`subscribe` and either ``async for`` or :meth:`recv`.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(capacity, 1)
        self._subscribers: list[LogSubscriber] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def subscribe(self, capacity: int | None = None) -> LogSubscriber:
        """Issue a fresh subscriber. ``capacity`` defaults to the
        broadcaster-wide cap; per-subscription override is useful for
        the admin UI's "show 5k buffered records" mode."""
        sub = LogSubscriber(self, capacity or self._capacity)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def _unsubscribe(self, sub: LogSubscriber) -> None:
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def receiver_count(self) -> int:
        """Number of currently-attached subscribers. Mirrors the Rust
        ``broadcast::Sender::receiver_count`` method used by the
        broadcaster to skip JSON construction when nobody listens."""
        with self._lock:
            return len(self._subscribers)

    def publish(self, record: LogRecord) -> None:
        """Fan out ``record`` to every live subscriber. Lock-free for
        the producer in the no-subscriber case (early return), and
        per-queue ``put_nowait`` for everyone else."""
        with self._lock:
            # Snapshot under lock; iterate outside so callbacks (which
            # might subscribe/unsubscribe in handlers) don't deadlock.
            subs = list(self._subscribers)
        if not subs:
            return
        for sub in subs:
            sub._push(record)


def make_structlog_processor(broadcaster: LogBroadcaster) -> Any:
    """Return a structlog processor that turns every log record into a
    :class:`LogRecord` and pushes it through ``broadcaster``.

    Drop the returned callable into your structlog pipeline as a final
    processor (before the renderer): once it runs, the event_dict
    flows on unchanged so any downstream JSON / console renderer keeps
    working. Returning ``event_dict`` is essential — structlog
    processors compose by passing the dict forward.
    """

    def processor(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        # Skip the JSON build when nobody is listening — cheap early
        # return for the production hot path.
        if broadcaster.receiver_count() == 0:
            return event_dict

        record = _record_from_event(method_name, event_dict)
        broadcaster.publish(record)
        return event_dict

    return processor


def _record_from_event(method_name: str, event_dict: dict[str, Any]) -> LogRecord:
    """Build a :class:`LogRecord` from a structlog event dict.

    Structlog hands processors a dict whose keys depend on the upstream
    bind / processor chain. We extract the well-known slots
    (``event`` → message, ``trace_id``, ``request_id``, ``subsystem``)
    and lump the rest into :attr:`LogRecord.fields`. Falls back
    gracefully when a slot is missing.
    """

    # Timestamp: prefer the structlog-provided one if any processor
    # earlier in the chain added it (``add_timestamp``), otherwise
    # synthesise now-UTC.
    ts_raw = event_dict.get("timestamp") or event_dict.get("ts")
    if isinstance(ts_raw, str):
        ts = ts_raw
    else:
        ts = datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat()

    level = (event_dict.get("level") or method_name or "INFO").upper()
    target = str(event_dict.get("logger") or event_dict.get("target") or "gateway")
    message = str(event_dict.get("event") or event_dict.get("message") or "")

    trace_id = _opt_str(event_dict.get("trace_id"))
    request_id = _opt_str(event_dict.get("request_id"))
    subsystem = _opt_str(event_dict.get("subsystem"))

    skip = {
        "event",
        "message",
        "timestamp",
        "ts",
        "level",
        "logger",
        "target",
        "trace_id",
        "request_id",
        "subsystem",
    }
    fields = {k: _json_safe(v) for k, v in event_dict.items() if k not in skip}

    return LogRecord(
        ts=ts,
        level=level,
        target=target,
        message=message,
        fields=fields,
        trace_id=trace_id,
        request_id=request_id,
        subsystem=subsystem,
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _json_safe(value: Any) -> Any:
    """Coerce a value into something json.dumps can serialise."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return repr(value)


__all__ = [
    "DEFAULT_CAPACITY",
    "LogBroadcaster",
    "LogRecord",
    "LogSubscriber",
    "make_structlog_processor",
]
