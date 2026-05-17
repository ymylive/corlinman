"""Tests for ``corlinman_channels.common`` (shared types).

Exercises the cross-cutting pieces (``InboundEvent``, ``ChannelBinding``
session-key stability, ``Attachment`` shape) so the per-channel test
modules can focus on transport-specific behaviour.
"""

from __future__ import annotations

import pytest
from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ChannelError,
    ConfigError,
    InboundAdapter,
    InboundEvent,
    TransportError,
    UnsupportedError,
)


class TestChannelBinding:
    """Builder + session-key stability."""

    def test_session_key_is_deterministic(self) -> None:
        a = ChannelBinding(channel="qq", account="100", thread="200", sender="300")
        b = ChannelBinding(channel="qq", account="100", thread="200", sender="300")
        assert a.session_key() == b.session_key()
        assert len(a.session_key()) == 16

    def test_session_key_differs_per_tuple(self) -> None:
        a = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        b = ChannelBinding(channel="qq", account="1", thread="2", sender="4")
        assert a.session_key() != b.session_key()

    def test_qq_group_builder(self) -> None:
        b = ChannelBinding.qq_group(100, 12345, 555)
        assert b.channel == "qq"
        assert b.account == "100"
        assert b.thread == "12345"
        assert b.sender == "555"

    def test_qq_private_uses_user_id_as_thread(self) -> None:
        b = ChannelBinding.qq_private(100, 555)
        assert b.thread == b.sender == "555"

    def test_telegram_user_id_defaults_to_chat_id(self) -> None:
        b = ChannelBinding.telegram(bot_id=999, chat_id=42)
        assert b.sender == "42"

    def test_telegram_user_id_overrides_chat_id(self) -> None:
        b = ChannelBinding.telegram(bot_id=999, chat_id=-100, user_id=77)
        assert b.thread == "-100"
        assert b.sender == "77"


class TestAttachment:
    """Attachment is a frozen dataclass so mutation should fail."""

    def test_image_url_attachment_round_trip(self) -> None:
        a = Attachment(
            kind=AttachmentKind.IMAGE,
            url="https://cdn/x.png",
            mime="image/*",
            file_name="x.png",
        )
        assert a.kind == AttachmentKind.IMAGE
        assert a.url == "https://cdn/x.png"
        assert a.data is None

    def test_attachment_is_frozen(self) -> None:
        a = Attachment(kind=AttachmentKind.AUDIO)
        with pytest.raises(Exception):
            a.kind = AttachmentKind.IMAGE  # type: ignore[misc]


class TestInboundEvent:
    """The normalized envelope is a frozen dataclass with sensible defaults."""

    def test_defaults_are_sane(self) -> None:
        binding = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        ev: InboundEvent[None] = InboundEvent(channel="qq", binding=binding, text="hi")
        assert ev.message_id is None
        assert ev.timestamp == 0
        assert ev.mentioned is False
        assert ev.attachments == []
        assert ev.payload is None
        assert ev.user_id is None

    def test_payload_is_generic(self) -> None:
        binding = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        ev: InboundEvent[dict] = InboundEvent(
            channel="qq", binding=binding, text="x", payload={"k": "v"}
        )
        assert ev.payload == {"k": "v"}


class TestErrors:
    """Error hierarchy: every concrete error inherits from ``ChannelError``."""

    @pytest.mark.parametrize(
        "cls", [ConfigError, TransportError, UnsupportedError]
    )
    def test_concrete_errors_inherit_from_base(self, cls: type[Exception]) -> None:
        assert issubclass(cls, ChannelError)

    def test_raise_and_catch_via_base(self) -> None:
        with pytest.raises(ChannelError):
            raise ConfigError("bad")


class TestInboundAdapterProtocol:
    """The Protocol is structural — any class with an ``inbound()`` method
    satisfies it without subclassing."""

    def test_protocol_check_succeeds_for_compliant_class(self) -> None:
        class Stub:
            def inbound(self):  # type: ignore[no-untyped-def]
                return iter([])

        assert isinstance(Stub(), InboundAdapter)

    def test_protocol_check_fails_without_inbound(self) -> None:
        class Stub:
            pass

        assert not isinstance(Stub(), InboundAdapter)
