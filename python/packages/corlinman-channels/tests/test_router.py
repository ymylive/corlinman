"""Tests for ``corlinman_channels.router``.

Mirrors the unit tests in ``rust/.../router.rs`` (the ``tests`` mod
inside the file). Adds the hook-bus mirror test to verify the
``HookEvent.RateLimitTriggered`` shape matches what the gateway-side
subscribers expect.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from corlinman_channels.onebot import (
    AtSegment,
    MessageEvent,
    MessageSegment,
    MessageType,
    Sender,
    TextSegment,
)
from corlinman_channels.rate_limit import TokenBucket
from corlinman_channels.router import (
    ChannelRouter,
    parse_group_keywords,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _group_event(
    raw: str,
    segs: list[MessageSegment],
    gid: int,
    *,
    user_id: int = 200,
    self_id: int = 100,
    message_id: int = 1,
) -> MessageEvent:
    return MessageEvent(
        self_id=self_id,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=gid,
        user_id=user_id,
        message_id=message_id,
        message=segs,
        raw_message=raw,
        time=1_700_000_000,
        sender=Sender(),
    )


# ---------------------------------------------------------------------------
# parse_group_keywords
# ---------------------------------------------------------------------------


class TestParseGroupKeywords:
    def test_parse_keywords_env_json(self) -> None:
        raw = '{"123":["a","b"],"456":["c"]}'
        m = parse_group_keywords(raw)
        assert m["123"] == ["a", "b"]
        assert m["456"] == ["c"]

    def test_parse_empty_env_returns_empty_map(self) -> None:
        assert parse_group_keywords("") == {}
        assert parse_group_keywords("   ") == {}

    def test_parse_non_object_payload_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_group_keywords("[1,2,3]")

    def test_parse_coerces_int_keys(self) -> None:
        # JSON keys are always strings — but be sure we tolerate the
        # accidental round-trip where someone passes a Python dict
        # with ints (via TOML or a misencoded payload).
        raw = '{"123":["x"]}'
        m = parse_group_keywords(raw)
        assert list(m.keys()) == ["123"]


# ---------------------------------------------------------------------------
# Keyword / mention gating
# ---------------------------------------------------------------------------


class TestKeywordAndMention:
    def test_dispatch_all_when_group_absent_from_map(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("随便聊聊", [TextSegment(text="随便聊聊")], 9999)
        req = router.dispatch(ev)
        assert req is not None
        assert req.content == "随便聊聊"
        assert req.binding.thread == "9999"

    def test_keyword_match_is_case_insensitive(self) -> None:
        router = ChannelRouter(
            group_keywords={"123": ["格兰", "Aemeath"]},
            self_ids=[100],
        )

        ev = _group_event("hey AEMEATH are you there", [], 123)
        assert router.dispatch(ev) is not None

        ev2 = _group_event("irrelevant chatter", [], 123)
        assert router.dispatch(ev2) is None

    def test_mention_bypasses_keyword_filter(self) -> None:
        router = ChannelRouter(
            group_keywords={"123": ["never_matches"]},
            self_ids=[100],
        )
        ev = _group_event(
            "[CQ:at,qq=100] help",
            [AtSegment(qq="100"), TextSegment(text=" help")],
            123,
        )
        req = router.dispatch(ev)
        assert req is not None
        assert req.mentioned is True

    def test_private_message_always_dispatches(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = MessageEvent(
            self_id=100,
            message_type=MessageType.PRIVATE,
            sub_type=None,
            group_id=None,
            user_id=77,
            message_id=1,
            message=[TextSegment(text="hi")],
            raw_message="hi",
            time=1,
            sender=None,
        )
        req = router.dispatch(ev)
        assert req is not None
        assert req.binding.channel == "qq"
        assert req.binding.thread == "77"

    def test_empty_group_message_drops(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev = _group_event("", [], 123)
        assert router.dispatch(ev) is None

    def test_session_key_stable_across_events(self) -> None:
        router = ChannelRouter(group_keywords={}, self_ids=[100])
        ev1 = _group_event("一号消息", [TextSegment(text="一号消息")], 321)
        ev2 = _group_event("二号消息", [TextSegment(text="二号消息")], 321)
        r1 = router.dispatch(ev1)
        r2 = router.dispatch(ev2)
        assert r1 is not None and r2 is not None
        assert r1.session_key == r2.session_key


# ---------------------------------------------------------------------------
# Rate-limit integration
# ---------------------------------------------------------------------------


def _count_hook() -> tuple[list[tuple[str, str]], object]:
    """Return a (calls list, hook callable) pair, mirroring the Rust
    ``count_hook`` helper. We use a list of tuples so tests can assert
    on the (channel, reason) labels as well as the count."""
    calls: list[tuple[str, str]] = []

    def hook(channel: str, reason: str) -> None:
        calls.append((channel, reason))

    return calls, hook


class TestRateLimitDispatch:
    def test_dispatch_drops_when_group_over_limit(self) -> None:
        group_bucket = TokenBucket.per_minute(1)
        calls, hook = _count_hook()
        router = (
            ChannelRouter(group_keywords={}, self_ids=[100])
            .with_rate_limits(group_bucket, None)
            .with_rate_limit_hook(hook)
        )
        ev1 = _group_event("msg1", [TextSegment(text="msg1")], 555)
        ev2 = _group_event("msg2", [TextSegment(text="msg2")], 555)
        assert router.dispatch(ev1) is not None, "first msg passes"
        assert router.dispatch(ev2) is None, "second msg dropped"
        assert len(calls) == 1
        assert calls[0] == ("qq", "group")

    def test_dispatch_drops_when_sender_over_limit(self) -> None:
        sender_bucket = TokenBucket.per_minute(1)
        calls, hook = _count_hook()
        router = (
            ChannelRouter(group_keywords={}, self_ids=[100])
            .with_rate_limits(None, sender_bucket)
            .with_rate_limit_hook(hook)
        )
        ev1 = _group_event("hi", [TextSegment(text="hi")], 777)
        ev2 = _group_event("hi again", [TextSegment(text="hi again")], 777)
        assert router.dispatch(ev1) is not None
        assert router.dispatch(ev2) is None
        assert len(calls) == 1
        assert calls[0] == ("qq", "sender")

    def test_rate_limit_drops_do_not_cross_groups(self) -> None:
        group_bucket = TokenBucket.per_minute(1)
        router = ChannelRouter(group_keywords={}, self_ids=[100]).with_rate_limits(
            group_bucket, None
        )
        a1 = _group_event("msg", [TextSegment(text="msg")], 1)
        a2 = _group_event("msg", [TextSegment(text="msg")], 1)
        b1 = _group_event("msg", [TextSegment(text="msg")], 2)
        assert router.dispatch(a1) is not None
        assert router.dispatch(a2) is None
        assert router.dispatch(b1) is not None, "group 2 has its own bucket"


# ---------------------------------------------------------------------------
# Hook-bus mirror
# ---------------------------------------------------------------------------


class TestHookBusMirror:
    @pytest.mark.asyncio
    async def test_rate_limit_drop_mirrors_to_hook_bus(self) -> None:
        """When a bus is attached, a rate-limit drop emits
        ``HookEvent.RateLimitTriggered`` to subscribers in addition to
        the legacy callback firing. Mirrors the Rust test of the same
        name."""
        from corlinman_hooks import HookBus, HookPriority
        from corlinman_hooks.event import _RateLimitTriggered

        group_bucket = TokenBucket.per_minute(1)
        calls, hook = _count_hook()
        bus = HookBus(16)
        sub = bus.subscribe(HookPriority.NORMAL)

        router = (
            ChannelRouter(group_keywords={}, self_ids=[100])
            .with_rate_limits(group_bucket, None)
            .with_rate_limit_hook(hook)
            .with_hook_bus(bus)
        )

        ev1 = _group_event("msg1", [TextSegment(text="msg1")], 555)
        ev2 = _group_event("msg2", [TextSegment(text="msg2")], 555)
        assert router.dispatch(ev1) is not None
        assert router.dispatch(ev2) is None

        # Legacy callback still fires once (back-compat contract).
        assert len(calls) == 1

        # And the bus subscriber observes the same event.
        got = await asyncio.wait_for(sub.recv(), timeout=1.0)
        assert isinstance(got, _RateLimitTriggered)
        # Group bucket trips → ``group_qq`` (reason_channel).
        assert got.limit_type == "group_qq"
        assert got.retry_after_ms == 0
        # ``ChannelBinding.session_key()`` is a 16-char sha256 prefix.
        sk = got.session_key()
        assert sk is not None
        assert len(sk) == 16

    def test_no_bus_preserves_legacy_rate_limit_behaviour(self) -> None:
        group_bucket = TokenBucket.per_minute(1)
        calls, hook = _count_hook()
        router = (
            ChannelRouter(group_keywords={}, self_ids=[100])
            .with_rate_limits(group_bucket, None)
            .with_rate_limit_hook(hook)
        )
        # No with_hook_bus() call.

        ev1 = _group_event("msg1", [TextSegment(text="msg1")], 555)
        ev2 = _group_event("msg2", [TextSegment(text="msg2")], 555)
        assert router.dispatch(ev1) is not None
        assert router.dispatch(ev2) is None
        assert len(calls) == 1
