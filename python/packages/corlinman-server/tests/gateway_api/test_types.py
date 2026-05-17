"""Unit tests for ``corlinman_server.gateway_api.types``.

Covers shape parity with the Rust ``corlinman-gateway-api`` crate:

* JSON round-trip for the pydantic models matches the Rust serde format.
* ``ChannelBinding.session_key`` matches the Rust 16-hex-char output for
  the canonical ``qq_group(100, 200, 300)`` fixture (the Rust test suite
  pins the same input).
* The event sum type discriminates correctly via ``isinstance`` and the
  ``.kind`` literal field.
* ``internal_chat_error_from_corlinman_error`` lifts ``reason``-bearing
  exceptions to a typed :class:`InternalChatError` and falls back to
  ``"unknown"`` for plain exceptions.
"""

from __future__ import annotations

import hashlib

import pytest
from corlinman_server.gateway_api import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
    ToolCallEvent,
    Usage,
    internal_chat_error_from_corlinman_error,
)


# ─── Enums ────────────────────────────────────────────────────────────


def test_role_enum_values_are_lowercase() -> None:
    """Wire values must match Rust ``#[serde(rename_all = "lowercase")]``."""
    assert Role.SYSTEM.value == "system"
    assert Role.USER.value == "user"
    assert Role.ASSISTANT.value == "assistant"
    assert Role.TOOL.value == "tool"


def test_attachment_kind_enum_values_are_lowercase() -> None:
    assert AttachmentKind.IMAGE.value == "image"
    assert AttachmentKind.AUDIO.value == "audio"
    assert AttachmentKind.VIDEO.value == "video"
    assert AttachmentKind.FILE.value == "file"


# ─── ChannelBinding ──────────────────────────────────────────────────


def test_channel_binding_session_key_matches_rust_algorithm() -> None:
    """``session_key`` must be the first 8 bytes of sha256, lowercase hex."""
    b = ChannelBinding(channel="qq", account="100", thread="200", sender="300")
    expected = hashlib.sha256(b"qq|100|200|300").digest()[:8].hex()
    assert b.session_key() == expected
    assert len(b.session_key()) == 16
    assert all(c in "0123456789abcdef" for c in b.session_key())


def test_channel_binding_session_key_is_stable() -> None:
    b1 = ChannelBinding.qq_group(100, 200, 300)
    b2 = ChannelBinding.qq_group(100, 200, 300)
    assert b1.session_key() == b2.session_key()


def test_channel_binding_session_key_differs_per_thread() -> None:
    a = ChannelBinding.qq_group(1, 2, 3).session_key()
    b = ChannelBinding.qq_group(1, 9, 3).session_key()
    assert a != b


def test_channel_binding_qq_private_uses_sender_as_thread() -> None:
    b = ChannelBinding.qq_private(10, 42)
    assert b.thread == "42"
    assert b.sender == "42"
    assert b.channel == "qq"


def test_channel_binding_channel_separates_keys() -> None:
    qq = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
    tg = ChannelBinding(channel="telegram", account="1", thread="2", sender="3")
    assert qq.session_key() != tg.session_key()


# ─── Pydantic round-trip ──────────────────────────────────────────────


def test_internal_chat_request_minimal_round_trip() -> None:
    req = InternalChatRequest(
        model="claude-sonnet",
        messages=[Message(role=Role.USER, content="hello")],
    )
    dumped = req.model_dump(mode="json")
    assert dumped["model"] == "claude-sonnet"
    assert dumped["messages"] == [{"role": "user", "content": "hello"}]
    assert dumped["session_key"] == ""
    assert dumped["stream"] is False
    # Rebuild from the JSON-mode dump.
    rebuilt = InternalChatRequest.model_validate(dumped)
    assert rebuilt == req


def test_internal_chat_request_with_binding_and_attachments() -> None:
    binding = ChannelBinding.qq_group(self_id=1, group_id=2, sender=3)
    req = InternalChatRequest(
        model="claude-sonnet",
        messages=[Message(role=Role.SYSTEM, content="be brief")],
        session_key=binding.session_key(),
        stream=True,
        max_tokens=128,
        temperature=0.4,
        attachments=[
            Attachment(kind=AttachmentKind.IMAGE, url="https://example/x.png")
        ],
        binding=binding,
    )
    dumped = req.model_dump(mode="json")
    assert dumped["binding"] == {
        "channel": "qq",
        "account": "1",
        "thread": "2",
        "sender": "3",
    }
    assert dumped["attachments"][0]["kind"] == "image"


def test_attachment_bytes_alias_round_trip() -> None:
    """``bytes_`` field serialises as ``"bytes"`` to match Rust serde."""
    a = Attachment(kind=AttachmentKind.AUDIO, bytes_=b"abc", mime="audio/wav")
    dumped = a.model_dump(by_alias=True)
    assert "bytes" in dumped
    assert "bytes_" not in dumped
    # Re-validate using the alias to confirm the round-trip works.
    rebuilt = Attachment.model_validate(
        {"kind": "audio", "bytes": b"abc", "mime": "audio/wav"}
    )
    assert rebuilt.bytes_ == b"abc"


def test_internal_chat_request_rejects_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        InternalChatRequest.model_validate(
            {"model": "x", "messages": [], "junk_field": True}
        )


# ─── Event sum type ───────────────────────────────────────────────────


def test_event_kind_literal_field() -> None:
    assert TokenDeltaEvent(text="hi").kind == "token_delta"
    assert ToolCallEvent(plugin="p", tool="t", args_json=b"{}").kind == "tool_call"
    assert DoneEvent(finish_reason="stop").kind == "done"
    assert (
        ErrorEvent(error=InternalChatError(reason="timeout", message="boom")).kind
        == "error"
    )


def test_done_event_carries_optional_usage() -> None:
    ev = DoneEvent(finish_reason="stop", usage=Usage(total_tokens=42))
    assert ev.usage is not None
    assert ev.usage.total_tokens == 42


def test_tool_call_event_args_json_is_bytes() -> None:
    """Matches Rust ``Bytes`` shape — no implicit decode."""
    ev = ToolCallEvent(plugin="p", tool="t", args_json=b'{"x":1}')
    assert isinstance(ev.args_json, bytes)
    assert ev.args_json.decode("utf-8") == '{"x":1}'


def test_events_are_frozen_dataclasses() -> None:
    """Cheap clone parity with Rust ``#[derive(Clone)]``."""
    ev = TokenDeltaEvent(text="hi")
    with pytest.raises(Exception):
        ev.text = "no"  # type: ignore[misc]


# ─── Error lift ───────────────────────────────────────────────────────


def test_internal_chat_error_from_reason_bearing_exception() -> None:
    class _FakeProviderError(Exception):
        reason = "rate_limit"

    err = internal_chat_error_from_corlinman_error(_FakeProviderError("slow down"))
    assert err.reason == "rate_limit"
    assert err.message == "slow down"


def test_internal_chat_error_from_plain_exception_falls_back_to_unknown() -> None:
    err = internal_chat_error_from_corlinman_error(RuntimeError("kaboom"))
    assert err.reason == "unknown"
    assert err.message == "kaboom"


def test_internal_chat_error_dataclass_is_hashable_and_frozen() -> None:
    """Frozen dataclass — required so :class:`ErrorEvent` stays clone-friendly."""
    err = InternalChatError(reason="timeout", message="boom")
    # Hashable: frozen + slots.
    _ = hash(err)
    with pytest.raises(Exception):
        err.reason = "other"  # type: ignore[misc]
