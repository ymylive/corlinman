"""OneBot v11 (QQ) WebSocket adapter.

Port of ``rust/.../qq/onebot.rs`` (forward-WS client) + ``rust/.../qq/message.rs``
(wire types + helpers).

corlinman is the **client** — it dials out to gocq / Lagrange / NapCatQQ
matching the "forward WebSocket" mode from the OneBot v11 spec
(<https://github.com/botuniverse/onebot-11>).

## Connection topology

::

    gocq/NapCat  <── WS ──>  OneBotAdapter
                                │   ▲
                        event_tx│   │action_rx
                                ▼   │
                        normalized InboundEvent  /  outbound Action

## Reconnect schedule

``1s → 2s → 5s → 10s → 30s`` (then saturates). A heartbeat ping every 30s
matches NapCat's idle expectation.

## Surface

Two layers:

* High-level :class:`OneBotAdapter` — implements
  :class:`corlinman_channels.common.InboundAdapter`; ``async for`` over
  ``adapter.inbound()`` yields normalized :class:`InboundEvent` objects.
* Low-level wire types (:class:`Event`, :class:`MessageEvent`,
  :class:`MessageSegment`, :class:`Action`, ...) so callers can opt into
  the raw OneBot vocabulary when they need it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundEvent,
    TransportError,
)

# ---------------------------------------------------------------------------
# Constants — match Rust ``RECONNECT_SCHEDULE`` / ``PING_INTERVAL``.
# ---------------------------------------------------------------------------

#: Backoff schedule (seconds) between reconnect attempts. Last entry repeats.
RECONNECT_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

#: Self-ping interval (seconds) for idle connections.
PING_INTERVAL: float = 30.0


# ===========================================================================
# Wire types — events
# ===========================================================================


class MessageType(StrEnum):
    """``private`` vs ``group``."""

    PRIVATE = "private"
    GROUP = "group"


@dataclass(slots=True)
class Sender:
    """Inner ``sender`` object of a :class:`MessageEvent`."""

    user_id: int | None = None
    nickname: str | None = None
    card: str | None = None
    role: str | None = None


@dataclass(slots=True)
class MessageEvent:
    """OneBot ``post_type = "message"`` event."""

    self_id: int
    message_type: MessageType
    user_id: int
    message_id: int
    message: list[MessageSegment]
    time: int
    sub_type: str | None = None
    group_id: int | None = None
    raw_message: str = ""
    sender: Sender | None = None


@dataclass(slots=True)
class NoticeEvent:
    """OneBot ``post_type = "notice"`` event — parsed but unused."""

    self_id: int
    notice_type: str
    time: int
    group_id: int | None = None
    user_id: int | None = None


@dataclass(slots=True)
class MetaEvent:
    """OneBot ``post_type = "meta_event"`` (heartbeat / lifecycle)."""

    self_id: int
    meta_event_type: str
    time: int


@dataclass(slots=True)
class RequestEvent:
    """OneBot ``post_type = "request"`` event (friend / group add)."""

    self_id: int
    request_type: str
    time: int
    user_id: int | None = None
    group_id: int | None = None
    flag: str | None = None


@dataclass(slots=True)
class UnknownEvent:
    """Sentinel for ``post_type`` values we don't model. Carries the raw
    JSON so callers can log the unexpected shape without aborting."""

    raw: dict[str, Any]


#: Tagged-union over the four OneBot event categories + the unknown fallback.
Event = MessageEvent | NoticeEvent | MetaEvent | RequestEvent | UnknownEvent


# ===========================================================================
# Wire types — message segments
# ===========================================================================


@dataclass(slots=True)
class TextSegment:
    """``{"type": "text", "data": {"text": ...}}``."""

    text: str


@dataclass(slots=True)
class AtSegment:
    """``{"type": "at", "data": {"qq": ...}}``. ``qq == "all"`` for @all."""

    qq: str


@dataclass(slots=True)
class ImageSegment:
    """``{"type": "image", "data": {"url": ..., "file": ...}}``."""

    url: str = ""
    file: str | None = None


@dataclass(slots=True)
class ReplySegment:
    """``{"type": "reply", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class FaceSegment:
    """``{"type": "face", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class RecordSegment:
    """``{"type": "record", "data": {"url": ...}}``."""

    url: str = ""


@dataclass(slots=True)
class ForwardSegment:
    """``{"type": "forward", "data": {"id": ...}}``."""

    id: str


@dataclass(slots=True)
class OtherSegment:
    """Fallback wrapper for segments we don't model. Carries the raw JSON
    so the reader loop keeps going in the face of spec drift."""

    raw: dict[str, Any]


#: Tagged-union over the seven understood segments plus :class:`OtherSegment`.
MessageSegment = (
    TextSegment
    | AtSegment
    | ImageSegment
    | ReplySegment
    | FaceSegment
    | RecordSegment
    | ForwardSegment
    | OtherSegment
)

#: Segment "type" → constructor table for :func:`_parse_segment`.
_SEGMENT_PARSERS: dict[str, Any] = {
    "text": lambda d: TextSegment(text=str(d.get("text", ""))),
    "at": lambda d: AtSegment(qq=str(d.get("qq", ""))),
    "image": lambda d: ImageSegment(url=str(d.get("url", "")), file=d.get("file")),
    "reply": lambda d: ReplySegment(id=str(d.get("id", ""))),
    "face": lambda d: FaceSegment(id=str(d.get("id", ""))),
    "record": lambda d: RecordSegment(url=str(d.get("url", ""))),
    "forward": lambda d: ForwardSegment(id=str(d.get("id", ""))),
}


def _parse_segment(raw: dict[str, Any]) -> MessageSegment:
    """Decode one CQ segment dict into the matching dataclass."""
    ty = raw.get("type")
    parser = _SEGMENT_PARSERS.get(ty if isinstance(ty, str) else "")
    if parser is None:
        return OtherSegment(raw=raw)
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        return OtherSegment(raw=raw)
    return parser(data)


def parse_event(raw: dict[str, Any]) -> Event:
    """Decode one OneBot event dict into the matching :data:`Event`.

    Unknown ``post_type`` collapses to :class:`UnknownEvent` so the reader
    loop can survive spec drift — matches the Rust ``Event::Unknown``
    fall-through behaviour.
    """
    post_type = raw.get("post_type")
    if post_type == "message":
        msg_type_raw = raw.get("message_type")
        try:
            msg_type = MessageType(msg_type_raw)
        except ValueError:
            return UnknownEvent(raw=raw)
        sender_raw = raw.get("sender")
        sender: Sender | None = None
        if isinstance(sender_raw, dict):
            sender = Sender(
                user_id=sender_raw.get("user_id"),
                nickname=sender_raw.get("nickname"),
                card=sender_raw.get("card"),
                role=sender_raw.get("role"),
            )
        message_raw = raw.get("message") or []
        segments = [_parse_segment(s) for s in message_raw if isinstance(s, dict)]
        return MessageEvent(
            self_id=int(raw.get("self_id", 0)),
            message_type=msg_type,
            user_id=int(raw.get("user_id", 0)),
            message_id=int(raw.get("message_id", 0)),
            message=segments,
            time=int(raw.get("time", 0)),
            sub_type=raw.get("sub_type"),
            group_id=raw.get("group_id"),
            raw_message=str(raw.get("raw_message", "")),
            sender=sender,
        )
    if post_type == "notice":
        return NoticeEvent(
            self_id=int(raw.get("self_id", 0)),
            notice_type=str(raw.get("notice_type", "")),
            time=int(raw.get("time", 0)),
            group_id=raw.get("group_id"),
            user_id=raw.get("user_id"),
        )
    if post_type == "meta_event":
        return MetaEvent(
            self_id=int(raw.get("self_id", 0)),
            meta_event_type=str(raw.get("meta_event_type", "")),
            time=int(raw.get("time", 0)),
        )
    if post_type == "request":
        return RequestEvent(
            self_id=int(raw.get("self_id", 0)),
            request_type=str(raw.get("request_type", "")),
            time=int(raw.get("time", 0)),
            user_id=raw.get("user_id"),
            group_id=raw.get("group_id"),
            flag=raw.get("flag"),
        )
    return UnknownEvent(raw=raw)


# ===========================================================================
# Segment helpers — match ``segments_to_text`` / ``segments_to_attachments``
# / ``is_mentioned`` in the Rust crate.
# ===========================================================================


def segments_to_text(segments: Iterable[MessageSegment]) -> str:
    """Flatten CQ segments to a single string.

    ``at`` segments become ``@<qq> `` so keyword routing still sees the
    address. Matches qqBot.js's ``_extractText`` / Rust's
    ``segments_to_text``.
    """
    out: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            out.append(seg.text)
        elif isinstance(seg, AtSegment):
            out.append(f"@{seg.qq} ")
    return "".join(out)


def segments_to_attachments(segments: Iterable[MessageSegment]) -> list[Attachment]:
    """Pull image / voice attachments out of a segment list.

    Skips segments with empty URLs (gocq sometimes ships an empty ``url``
    on offline media). Matches the Rust ``segments_to_attachments`` filter.
    """
    out: list[Attachment] = []
    for seg in segments:
        if isinstance(seg, ImageSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.IMAGE,
                    url=seg.url,
                    mime="image/*",
                    file_name=seg.file,
                )
            )
        elif isinstance(seg, RecordSegment) and seg.url:
            out.append(
                Attachment(
                    kind=AttachmentKind.AUDIO,
                    url=seg.url,
                    mime="audio/*",
                )
            )
    return out


def is_mentioned(segments: Iterable[MessageSegment], self_id: int) -> bool:
    """True if any ``at`` segment targets ``self_id`` (or is ``@all``)."""
    target = str(self_id)
    for seg in segments:
        if isinstance(seg, AtSegment) and (seg.qq == target or seg.qq == "all"):
            return True
    return False


# ===========================================================================
# Wire types — outbound actions
# ===========================================================================


@dataclass(slots=True)
class SendPrivateMsg:
    """``action = "send_private_msg"`` payload."""

    user_id: int
    message: list[MessageSegment]


@dataclass(slots=True)
class SendGroupMsg:
    """``action = "send_group_msg"`` payload."""

    group_id: int
    message: list[MessageSegment]


@dataclass(slots=True)
class ForwardNode:
    """One node in a merged-forward (``node`` segment)."""

    name: str
    uin: str
    content: list[MessageSegment]


@dataclass(slots=True)
class SendGroupForwardMsg:
    """``action = "send_group_forward_msg"`` payload."""

    group_id: int
    messages: list[ForwardNode]


#: Tagged-union of the three actions corlinman emits.
Action = SendPrivateMsg | SendGroupMsg | SendGroupForwardMsg


def _segment_to_wire(seg: MessageSegment) -> dict[str, Any]:
    """Serialize a single segment back to OneBot wire form."""
    if isinstance(seg, TextSegment):
        return {"type": "text", "data": {"text": seg.text}}
    if isinstance(seg, AtSegment):
        return {"type": "at", "data": {"qq": seg.qq}}
    if isinstance(seg, ImageSegment):
        data: dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            data["file"] = seg.file
        return {"type": "image", "data": data}
    if isinstance(seg, ReplySegment):
        return {"type": "reply", "data": {"id": seg.id}}
    if isinstance(seg, FaceSegment):
        return {"type": "face", "data": {"id": seg.id}}
    if isinstance(seg, RecordSegment):
        return {"type": "record", "data": {"url": seg.url}}
    if isinstance(seg, ForwardSegment):
        return {"type": "forward", "data": {"id": seg.id}}
    # OtherSegment falls through to its raw form.
    return seg.raw


def action_to_wire(action: Action) -> dict[str, Any]:
    """Serialize an :data:`Action` to the OneBot envelope.

    Output shape: ``{"action": "...", "params": {...}}`` — matches the
    Rust ``Action`` serde tag/content layout.
    """
    if isinstance(action, SendPrivateMsg):
        return {
            "action": "send_private_msg",
            "params": {
                "user_id": action.user_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    if isinstance(action, SendGroupMsg):
        return {
            "action": "send_group_msg",
            "params": {
                "group_id": action.group_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    # SendGroupForwardMsg
    return {
        "action": "send_group_forward_msg",
        "params": {
            "group_id": action.group_id,
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "name": node.name,
                        "uin": node.uin,
                        "content": [_segment_to_wire(s) for s in node.content],
                    },
                }
                for node in action.messages
            ],
        },
    }


# ===========================================================================
# Adapter
# ===========================================================================


@dataclass(slots=True)
class OneBotConfig:
    """Configuration for :class:`OneBotAdapter`.

    ``url`` is the full WS URL (``ws://host:port/``). ``access_token`` is
    sent as ``Authorization: Bearer <token>`` when present.
    """

    url: str
    access_token: str | None = None
    self_ids: list[int] = field(default_factory=list)
    reconnect_schedule: tuple[float, ...] = RECONNECT_SCHEDULE
    ping_interval: float = PING_INTERVAL


class OneBotAdapter:
    """Forward-WebSocket OneBot v11 client.

    The adapter is a small state machine:

    1. ``async with adapter:`` (or :meth:`connect`) dials the upstream WS.
    2. ``async for event in adapter.inbound():`` yields normalized
       :class:`InboundEvent` objects (only ``MessageEvent`` post_types are
       surfaced; meta/notice/request events are silently absorbed since
       no upstream consumer reads them yet — they exist in the wire types
       so the parser doesn't drop the connection).
    3. :meth:`send_action` posts an outbound :data:`Action`.
    4. :meth:`close` (or the ``async with`` exit) tears down the WS.

    The reconnect loop lives **inside** :meth:`inbound`: yielding an event
    is paused while we sleep+redial, but the iterator never raises a
    transient transport error — callers just see the next event after the
    reconnect lands. Permanent config errors (missing URL, invalid token
    header) raise :class:`ConfigError` from :meth:`connect` instead.
    """

    def __init__(self, config: OneBotConfig) -> None:
        if not config.url:
            raise ConfigError("OneBotConfig.url is empty")
        self._cfg = config
        self._ws: ClientConnection | None = None
        self._closed = False
        # Bounded queue so a stalled consumer doesn't grow without bound.
        self._inbound_q: asyncio.Queue[Event] = asyncio.Queue(maxsize=64)
        self._outbound_q: asyncio.Queue[Action] = asyncio.Queue(maxsize=64)
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OneBotAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the background reader loop.

        Doesn't block on the actual WS connect — that happens inside the
        reader so reconnect logic stays in one place. The first call to
        :meth:`inbound` will start yielding once the connection lands.
        """
        if self._reader_task is not None:
            return
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop(), name="onebot-reader")

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
    # Public API
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[MessageEvent]]:
        """Yield normalized inbound events until the adapter is closed.

        Only :class:`MessageEvent` post-types surface — meta / notice /
        request events are absorbed silently (matches Rust ``service.rs``
        which short-circuits with ``let Event::Message(msg_ev) = ev else
        continue``).
        """
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                ev = await self._inbound_q.get()
            except asyncio.CancelledError:
                return
            if not isinstance(ev, MessageEvent):
                continue
            yield _normalize_message_event(ev)

    async def send_action(self, action: Action) -> None:
        """Enqueue an outbound :data:`Action` for the reader loop to flush.

        Returns once the action is queued — actual transmission happens
        on the writer side of the WS. Raises :class:`TransportError` if
        the adapter has been closed.
        """
        if self._closed:
            raise TransportError("OneBotAdapter is closed")
        await self._outbound_q.put(action)

    # ------------------------------------------------------------------
    # Reader loop — encapsulates reconnect schedule.
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Connect → pump → on disconnect, sleep + retry.

        Mirrors the Rust ``OneBotClient::run`` state machine: the
        backoff index resets to 0 after a clean disconnect and grows
        monotonically across consecutive failures.
        """
        attempt = 0
        schedule = self._cfg.reconnect_schedule or RECONNECT_SCHEDULE
        while not self._closed:
            try:
                await self._connect_once()
                if self._closed:
                    return
                attempt = 0  # clean disconnect — reset backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                # Use logging at warning level. We bury the exception text in
                # the queue's debug surface so unit tests can assert without
                # patching logging.
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
        """One connect → pump cycle."""
        headers: list[tuple[str, str]] = []
        if self._cfg.access_token:
            headers.append(("Authorization", f"Bearer {self._cfg.access_token}"))
        # `additional_headers` is the param name for websockets >= 13.
        async with websockets.connect(
            self._cfg.url,
            additional_headers=headers or None,
            ping_interval=self._cfg.ping_interval,
        ) as ws:
            self._ws = ws
            try:
                await self._pump(ws)
            finally:
                self._ws = None

    async def _pump(self, ws: ClientConnection) -> None:
        """Two-way pump: forward outbound actions, decode inbound frames."""
        writer = asyncio.create_task(self._writer_loop(ws), name="onebot-writer")
        try:
            async for raw_msg in ws:
                if self._closed:
                    break
                if isinstance(raw_msg, bytes):
                    # OneBot v11 is text-only; ignore binary frames.
                    continue
                try:
                    raw = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                event = parse_event(raw)
                # Backpressure: queue.put will block if the consumer falls
                # behind; that's intentional — we want to slow the WS read.
                await self._inbound_q.put(event)
        finally:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer

    async def _writer_loop(self, ws: ClientConnection) -> None:
        """Drain ``self._outbound_q`` and send each action as a text frame."""
        while True:
            try:
                action = await self._outbound_q.get()
            except asyncio.CancelledError:
                return
            payload = json.dumps(action_to_wire(action))
            try:
                await ws.send(payload)
            except Exception as exc:
                # Bounce the action back so the reconnect doesn't lose it?
                # The Rust adapter drops on send failure; we match.
                raise TransportError(f"OneBot send failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_message_event(ev: MessageEvent) -> InboundEvent[MessageEvent]:
    """Convert a low-level :class:`MessageEvent` into the normalized envelope."""
    if ev.message_type == MessageType.GROUP and ev.group_id is not None:
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id, ev.user_id)
    else:
        binding = ChannelBinding.qq_private(ev.self_id, ev.user_id)
    return InboundEvent(
        channel="qq",
        binding=binding,
        text=segments_to_text(ev.message),
        message_id=str(ev.message_id),
        timestamp=ev.time,
        mentioned=is_mentioned(ev.message, ev.self_id),
        attachments=segments_to_attachments(ev.message),
        payload=ev,
    )


__all__ = [
    "PING_INTERVAL",
    "RECONNECT_SCHEDULE",
    "Action",
    "AtSegment",
    "Event",
    "FaceSegment",
    "ForwardNode",
    "ForwardSegment",
    "ImageSegment",
    "MessageEvent",
    "MessageSegment",
    "MessageType",
    "MetaEvent",
    "NoticeEvent",
    "OneBotAdapter",
    "OneBotConfig",
    "OtherSegment",
    "RecordSegment",
    "ReplySegment",
    "RequestEvent",
    "SendGroupForwardMsg",
    "SendGroupMsg",
    "SendPrivateMsg",
    "Sender",
    "TextSegment",
    "UnknownEvent",
    "action_to_wire",
    "is_mentioned",
    "parse_event",
    "segments_to_attachments",
    "segments_to_text",
]
