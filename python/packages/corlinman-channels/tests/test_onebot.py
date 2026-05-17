"""Tests for ``corlinman_channels.onebot``.

Two layers:

* Wire-level tests (``parse_event``, ``segments_to_*``, ``action_to_wire``)
  — mirror the ``#[cfg(test)] mod tests`` block in ``rust/.../qq/message.rs``.
* End-to-end tests against an in-process WebSocket server (``websockets``
  fixture in ``conftest.py``) — mirror ``tests/onebot_integration.rs``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from corlinman_channels.common import AttachmentKind, ConfigError
from corlinman_channels.onebot import (
    AtSegment,
    FaceSegment,
    ImageSegment,
    MessageEvent,
    MessageType,
    MetaEvent,
    OneBotAdapter,
    OneBotConfig,
    OtherSegment,
    RecordSegment,
    ReplySegment,
    SendGroupMsg,
    TextSegment,
    UnknownEvent,
    action_to_wire,
    is_mentioned,
    parse_event,
    segments_to_attachments,
    segments_to_text,
)
from websockets.asyncio.server import ServerConnection

# ---------------------------------------------------------------------------
# Wire-type tests (parse + serialise).
# ---------------------------------------------------------------------------


class TestParseEvent:
    """``parse_event`` recognises the four documented post types and falls
    through to :class:`UnknownEvent` for anything else."""

    def test_group_message_event(self) -> None:
        raw: dict[str, Any] = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": 1_700_000_000,
            "self_id": 100,
            "user_id": 200,
            "group_id": 300,
            "message_id": 1,
            "message": [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
            "raw_message": "[CQ:at,qq=100] hello",
            "sender": {"user_id": 200, "nickname": "alice"},
        }
        ev = parse_event(raw)
        assert isinstance(ev, MessageEvent)
        assert ev.message_type == MessageType.GROUP
        assert ev.group_id == 300
        assert len(ev.message) == 2
        assert ev.sender is not None and ev.sender.nickname == "alice"
        assert is_mentioned(ev.message, 100)

    def test_heartbeat_decodes_as_meta_event(self) -> None:
        raw = {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": 1_700_000_000,
            "self_id": 100,
            "interval": 5000,
            "status": {},
        }
        ev = parse_event(raw)
        assert isinstance(ev, MetaEvent)
        assert ev.meta_event_type == "heartbeat"

    def test_unknown_post_type_maps_to_unknown_event(self) -> None:
        raw = {"post_type": "mystery", "time": 0, "self_id": 0}
        ev = parse_event(raw)
        assert isinstance(ev, UnknownEvent)
        assert ev.raw["post_type"] == "mystery"


class TestSegments:
    """Match the seven understood segment types + the ``Other`` fall-through."""

    @pytest.mark.parametrize(
        ("payload", "expected_cls"),
        [
            ({"type": "text", "data": {"text": "hi"}}, TextSegment),
            ({"type": "at", "data": {"qq": "1"}}, AtSegment),
            ({"type": "image", "data": {"url": "https://x", "file": "f"}}, ImageSegment),
            ({"type": "reply", "data": {"id": "42"}}, ReplySegment),
            ({"type": "face", "data": {"id": "1"}}, FaceSegment),
            ({"type": "record", "data": {"url": "https://y"}}, RecordSegment),
        ],
    )
    def test_seven_segment_types(self, payload: dict[str, Any], expected_cls: type) -> None:
        ev = parse_event({"post_type": "message", "message_type": "private",
                          "self_id": 1, "user_id": 1, "message_id": 1,
                          "message": [payload], "time": 0})
        assert isinstance(ev, MessageEvent)
        assert len(ev.message) == 1
        assert isinstance(ev.message[0], expected_cls)

    def test_unknown_segment_collapses_to_other(self) -> None:
        ev = parse_event({"post_type": "message", "message_type": "private",
                          "self_id": 1, "user_id": 1, "message_id": 1,
                          "message": [{"type": "video", "data": {"url": "x"}}], "time": 0})
        assert isinstance(ev, MessageEvent)
        assert isinstance(ev.message[0], OtherSegment)


class TestSegmentHelpers:
    """``segments_to_text`` / ``segments_to_attachments`` / ``is_mentioned``."""

    def test_text_extraction_flattens_segments(self) -> None:
        segs = [
            AtSegment(qq="100"),
            TextSegment(text="hello "),
            TextSegment(text="world"),
            FaceSegment(id="1"),
        ]
        t = segments_to_text(segs)
        assert "hello world" in t
        assert "@100" in t

    def test_attachments_cover_image_and_record(self) -> None:
        segs = [
            TextSegment(text="caption"),
            ImageSegment(url="https://cdn/img.jpg", file="img.jpg"),
            RecordSegment(url="https://cdn/voice.amr"),
            OtherSegment(raw={"type": "video"}),
            AtSegment(qq="100"),
            FaceSegment(id="1"),
            ReplySegment(id="42"),
        ]
        atts = segments_to_attachments(segs)
        assert len(atts) == 2
        assert atts[0].kind == AttachmentKind.IMAGE
        assert atts[0].url == "https://cdn/img.jpg"
        assert atts[0].file_name == "img.jpg"
        assert atts[1].kind == AttachmentKind.AUDIO

    def test_attachments_skip_empty_urls(self) -> None:
        segs = [ImageSegment(url="", file=None)]
        assert segments_to_attachments(segs) == []

    def test_attachments_empty_for_text_only(self) -> None:
        segs = [TextSegment(text="hi"), AtSegment(qq="100")]
        assert segments_to_attachments(segs) == []

    def test_is_mentioned_handles_at_all(self) -> None:
        segs = [AtSegment(qq="all")]
        assert is_mentioned(segs, 12345)

    def test_is_mentioned_returns_false_when_unmentioned(self) -> None:
        segs = [TextSegment(text="hi there")]
        assert not is_mentioned(segs, 100)


class TestActionToWire:
    """Serialised actions match the OneBot envelope shape."""

    def test_send_group_msg_envelope(self) -> None:
        a = SendGroupMsg(
            group_id=1,
            message=[ReplySegment(id="42"), TextSegment(text="hello")],
        )
        s = action_to_wire(a)
        assert s["action"] == "send_group_msg"
        assert s["params"]["group_id"] == 1
        assert s["params"]["message"][0]["type"] == "reply"
        assert s["params"]["message"][0]["data"]["id"] == "42"
        assert s["params"]["message"][1]["type"] == "text"


# ---------------------------------------------------------------------------
# Adapter-level tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_empty_url_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            OneBotAdapter(OneBotConfig(url=""))


# ---------------------------------------------------------------------------
# WebSocket integration tests
# ---------------------------------------------------------------------------


class TestOneBotIntegration:
    """End-to-end tests against an in-process ``websockets`` server."""

    async def test_adapter_yields_normalized_event(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            # Push one group message; then keep the connection open until
            # the client closes.
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 100,
                "user_id": 555,
                "group_id": 12345,
                "message_id": 42,
                "message": [
                    {"type": "at", "data": {"qq": "100"}},
                    {"type": "text", "data": {"text": "hello"}},
                ],
                "raw_message": "@100 hello",
                "time": 1_700_000_000,
            }))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url, self_ids=[100]))
            async with adapter:
                # Pull one event with a generous timeout to absorb connect.
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                assert ev.channel == "qq"
                assert ev.binding.account == "100"
                assert ev.binding.thread == "12345"
                assert ev.binding.sender == "555"
                assert ev.mentioned is True
                assert "hello" in ev.text
                assert ev.message_id == "42"
                assert isinstance(ev.payload, MessageEvent)

    async def test_adapter_drops_non_message_events(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            # First a heartbeat (meta event — should be filtered),
            # then a real message.
            await ws.send(json.dumps({
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "self_id": 100,
                "time": 1,
            }))
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "private",
                "self_id": 100,
                "user_id": 200,
                "message_id": 7,
                "message": [{"type": "text", "data": {"text": "yo"}}],
                "time": 2,
            }))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url))
            async with adapter:
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                # The heartbeat was filtered out; the only surfaced event is
                # the private message.
                assert ev.binding.channel == "qq"
                assert ev.binding.account == "100"
                assert ev.text == "yo"

    async def test_send_action_round_trips_through_ws(self, ws_server) -> None:
        received: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        received.append(raw.decode("utf-8"))
                    else:
                        received.append(raw)
                    break
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = OneBotAdapter(OneBotConfig(url=url))
            async with adapter:
                # Allow the initial connect to complete.
                await asyncio.sleep(0.1)
                await adapter.send_action(
                    SendGroupMsg(
                        group_id=10, message=[TextSegment(text="hi")]
                    )
                )
                # Give the writer task a moment to flush.
                for _ in range(20):
                    if received:
                        break
                    await asyncio.sleep(0.05)

        assert received, "server never received a frame"
        payload = json.loads(received[0])
        assert payload["action"] == "send_group_msg"
        assert payload["params"]["group_id"] == 10
        assert payload["params"]["message"][0]["data"]["text"] == "hi"
