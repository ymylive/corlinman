"""Shared types for inbound channel adapters.

Mirrors the cross-cutting pieces of ``rust/crates/corlinman-channels/src/``:

* :class:`InboundEvent` — the normalized envelope each adapter yields.
* :class:`ChannelBinding` — transport-agnostic ``(channel, account, thread,
  sender)`` tuple, matching ``corlinman_core::channel_binding::ChannelBinding``.
* :class:`Attachment` / :class:`AttachmentKind` — multimodal attachment
  metadata (mirrors ``corlinman_gateway_api::Attachment``).
* :class:`ChannelError` — typed error surface for adapter operations.

Keeping these in one module means the per-channel adapters (``onebot``,
``logstream``, ``telegram``) all consume the same envelope shape and an
``async for`` loop over the ``inbound`` async-iterator yields a uniform
object regardless of transport.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

# Re-export UserId so downstream consumers can write
# ``from corlinman_channels.common import UserId`` without a separate import.
# Soft dependency — corlinman-identity is the W1 package this one builds on.
from corlinman_identity import UserId

# ---------------------------------------------------------------------------
# Attachment metadata (mirrors corlinman_gateway_api::Attachment)
# ---------------------------------------------------------------------------


class AttachmentKind(StrEnum):
    """Coarse-grained classification of a multimodal payload.

    Matches the Rust ``AttachmentKind`` enum (``image`` / ``audio`` /
    ``video`` / ``document``). String values are the canonical wire shape;
    the gateway routes these to the provider's multimodal handler.
    """

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class Attachment:
    """One inbound multimodal attachment.

    Either ``url`` or ``data`` is populated depending on whether the transport
    pre-uploaded the asset to a CDN (OneBot's ``image`` segment carries a
    URL) or shipped raw bytes (Telegram requires a follow-up download). The
    ``mime`` is best-effort; QQ doesn't expose a precise content type so we
    fall back to ``image/*`` / ``audio/*`` glob form.
    """

    kind: AttachmentKind
    url: str | None = None
    data: bytes | None = None
    mime: str | None = None
    file_name: str | None = None


# ---------------------------------------------------------------------------
# ChannelBinding — transport-agnostic conversation locus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """Transport-agnostic conversation locus.

    Mirrors ``corlinman_core::channel_binding::ChannelBinding`` (Rust). The
    four-tuple ``(channel, account, thread, sender)`` is hashed into a
    16-hex-char ``session_key`` which downstream RAG / approval logic uses
    as the stable conversation key.

    Examples:
    - QQ group: ``ChannelBinding("qq", "<bot_qq>", "<group_id>", "<user_qq>")``
    - QQ private: ``thread == sender`` (the user's QQ id).
    - Telegram: ``ChannelBinding("telegram", "<bot_id>", "<chat_id>",
      "<user_id>")``; private chats also have ``thread == sender``.
    """

    channel: str
    account: str
    thread: str
    sender: str

    def session_key(self) -> str:
        """Deterministic 16-hex-char digest of the four-tuple.

        Truncated SHA-256; collisions are vanishingly rare across the
        identifier space we use. Stable across processes so two replicas of
        the gateway compute the same key for the same binding.
        """
        digest = hashlib.sha256(
            f"{self.channel}|{self.account}|{self.thread}|{self.sender}".encode()
        ).hexdigest()
        return digest[:16]

    @classmethod
    def qq_group(cls, self_id: int | str, group_id: int | str, user_id: int | str) -> ChannelBinding:
        """Builder mirroring ``ChannelBinding::qq_group`` in Rust."""
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(group_id),
            sender=str(user_id),
        )

    @classmethod
    def qq_private(cls, self_id: int | str, user_id: int | str) -> ChannelBinding:
        """Builder mirroring ``ChannelBinding::qq_private`` in Rust.

        Per the QQ adapter convention, private-chat threads use the peer
        user id as both ``thread`` and ``sender`` so session keys remain
        stable per-peer.
        """
        return cls(
            channel="qq",
            account=str(self_id),
            thread=str(user_id),
            sender=str(user_id),
        )

    @classmethod
    def telegram(
        cls,
        bot_id: int | str,
        chat_id: int | str,
        user_id: int | str | None = None,
    ) -> ChannelBinding:
        """Builder for Telegram messages.

        ``user_id`` defaults to ``chat_id`` when absent (anonymous channel
        posts). Matches the fallback in
        ``rust/.../telegram/message.rs::binding_from_message``.
        """
        sender = user_id if user_id is not None else chat_id
        return cls(
            channel="telegram",
            account=str(bot_id),
            thread=str(chat_id),
            sender=str(sender),
        )


# ---------------------------------------------------------------------------
# Normalized inbound event
# ---------------------------------------------------------------------------

#: Payload type variable for :class:`InboundEvent`. The per-channel adapters
#: parametrize this so callers that only care about the normalized envelope
#: can write ``AsyncIterator[InboundEvent[Any]]``, while a caller that wants
#: the raw OneBot ``MessageEvent`` can keep the precise type.
PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True)
class InboundEvent(Generic[PayloadT]):
    """Normalized inbound event yielded by every channel adapter.

    Designed so a generic consumer can write::

        async for event in adapter.inbound():
            print(event.channel, event.text, event.binding.session_key())

    without knowing whether the source is QQ, Telegram, or a log stream.

    Adapters fill ``text`` with the human-readable content (flattened from
    CQ segments / Telegram entities) and ``payload`` with the raw transport
    event so callers can downcast when they need richer details.
    """

    channel: str
    """Channel slug (``"qq"``, ``"telegram"``, ``"logstream"``)."""

    binding: ChannelBinding
    """Transport-agnostic conversation locus."""

    text: str
    """Best-effort plain-text content. May be empty (e.g. an image-only
    message). Consumers that need richer structure read ``payload``."""

    message_id: str | None = None
    """Transport-specific message id (``str`` so 64-bit QQ ids round-trip
    safely; Telegram ids fit too)."""

    timestamp: int = 0
    """Unix seconds. Falls back to 0 when the transport doesn't expose one
    (LogStream frames sometimes lack timestamps)."""

    mentioned: bool = False
    """True when the bot was @-addressed (group / supergroup); always
    ``True`` for private chats since every DM is implicitly addressed."""

    attachments: list[Attachment] = field(default_factory=list)
    """Multimodal payload metadata; empty for text-only messages."""

    payload: PayloadT | None = None
    """Raw transport event for callers that need to introspect further. The
    concrete shape is documented per adapter module."""

    user_id: UserId | None = None
    """Optional canonical :class:`UserId` if the adapter was wired with an
    identity resolver. Adapters that don't perform resolution leave this
    ``None``; the caller can do it lazily via the binding."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChannelError(Exception):
    """Base error for channel adapter operations.

    Mirrors the Rust ``ChannelError`` enum — concrete subclasses below
    cover the cases the adapters actually surface today.
    """


class ConfigError(ChannelError):
    """Adapter configuration is invalid (missing token, empty URL, ...)."""


class TransportError(ChannelError):
    """Underlying transport failed (WS disconnect we cannot recover from,
    Telegram returned 4xx, etc.)."""


class UnsupportedError(ChannelError):
    """Operation not supported by this adapter (read-only channels that
    do not implement outbound send, etc.)."""


# ---------------------------------------------------------------------------
# InboundAdapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InboundAdapter(Protocol):
    """Structural protocol every channel adapter satisfies.

    The minimal contract is just ``inbound()`` returning an async iterator
    of :class:`InboundEvent`. Adapters typically also implement ``__aenter__``
    / ``__aexit__`` for connection lifecycle, but the protocol does not
    require it so callers can wrap pre-connected fixtures in tests.
    """

    def inbound(self) -> AsyncIterator[InboundEvent[Any]]:
        """Yield normalized inbound events until the adapter is closed."""
        ...


__all__ = [
    "Attachment",
    "AttachmentKind",
    "ChannelBinding",
    "ChannelError",
    "ConfigError",
    "InboundAdapter",
    "InboundEvent",
    "TransportError",
    "UnsupportedError",
    "UserId",
]
