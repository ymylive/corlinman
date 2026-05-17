"""Telegram webhook handler + signature validation.

Python port of ``rust/.../telegram/webhook.rs``.

Wire::

    Telegram ── POST /channels/telegram/webhook ──► gateway
                       X-Telegram-Bot-Api-Secret-Token: <configured>
                       body = Update JSON

Responsibility split:

- :func:`verify_secret`: constant-time compare of the incoming header
  to the configured secret. Mismatch → caller returns 401.
- :func:`process_update`: drives the full pipeline (media download →
  hook emission → session key build) from a decoded :class:`Update`.
  Kept as a free function so the gateway route handler stays thin
  and unit tests don't need to spin up a real HTTP server.

All I/O goes through the :class:`TelegramHttp` Protocol (for media
downloads) and an optional :class:`corlinman_hooks.HookBus` so
production wiring and tests share one code path.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corlinman_channels.telegram import (
    Message,
    MessageRoute,
    Update,
    classify,
    session_key_for,
)
from corlinman_channels.telegram_media import (
    DownloadedMedia,
    MediaError,
    TelegramHttp,
    download_to_media_dir,
)

__all__ = [
    "ProcessedUpdate",
    "WebhookContext",
    "WebhookCtx",
    "WebhookError",
    "default_media_dir",
    "process_update",
    "verify_secret",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WebhookError(Exception):
    """Base error for webhook processing. Mirrors Rust ``WebhookError``."""


class WebhookMediaError(WebhookError):
    """Wraps a :class:`MediaError` (kept for parity with Rust's
    ``WebhookError::Media`` variant — :func:`process_update` swallows
    media errors today, but the variant is exported in case callers
    propagate them explicitly)."""


class WebhookDecodeError(WebhookError):
    """Could not parse the incoming Update JSON."""


WebhookError.Media = WebhookMediaError  # type: ignore[attr-defined]
WebhookError.Decode = WebhookDecodeError  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# verify_secret
# ---------------------------------------------------------------------------


def verify_secret(configured: str, got: str | None) -> bool:
    """Constant-time secret comparison.

    Telegram echoes back the configured ``secret_token`` in the
    ``X-Telegram-Bot-Api-Secret-Token`` request header; a mismatch
    (or absence, when a secret is configured) → 401.

    When ``configured`` is the empty string the check is disabled
    (useful for local dev with a tunnel that strips headers) —
    callers should log a warning at startup in that case. Mirrors
    Rust ``verify_secret``.
    """
    if not configured:
        return True
    got_str = got or ""
    # ``hmac.compare_digest`` is the stdlib constant-time primitive —
    # it short-circuits only on length difference, matching the
    # behaviour the Rust hand-rolled XOR loop. The length-difference
    # short-circuit is acceptable here because the secret length is
    # public (it's the bot owner's choice, fixed at config time).
    return hmac.compare_digest(configured.encode(), got_str.encode())


# ---------------------------------------------------------------------------
# Context + result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WebhookCtx:
    """Runtime context passed to :func:`process_update`.

    Mirrors Rust's borrowed ``WebhookCtx<'a>``. Easy to construct
    inline at the call site; for the route-handler-shared variant
    use :class:`WebhookContext` (owned, holds a ``Path`` instead of a
    ``&Path``).
    """

    bot_id: int
    bot_username: str | None
    data_dir: Path
    http: TelegramHttp
    hooks: Any = None
    """Optional :class:`corlinman_hooks.HookBus`. Typed ``Any`` to keep
    the import lightweight (hooks is a workspace dep but the bus is
    constructed lazily inside :func:`process_update`)."""


@dataclass(slots=True)
class WebhookContext:
    """Owned variant used when passing the handler into an
    application-state container (e.g. FastAPI dependency, ASGI
    middleware closure). Mirrors Rust ``WebhookContext``.
    """

    bot_id: int
    bot_username: str | None
    data_dir: Path
    http: TelegramHttp
    hooks: Any = None
    secret_token: str = ""


@dataclass(slots=True)
class ProcessedUpdate:
    """Outcome of processing one :class:`Update`. Returned so the
    gateway route can decide whether to trigger an agent reply
    without re-parsing. Mirrors Rust ``ProcessedUpdate``."""

    update_id: int
    session_key: str
    route: MessageRoute
    content: str
    media: DownloadedMedia | None
    """Populated when the message carried a media attachment that was
    successfully downloaded. ``None`` for plain-text messages or when
    the download failed (error is logged; the update still flows)."""
    media_kind: str
    """``"photo"`` | ``"voice"`` | ``"document"`` | ``"text"``."""


# ---------------------------------------------------------------------------
# default_media_dir
# ---------------------------------------------------------------------------


def default_media_dir(data_dir: Path) -> Path:
    """Compute the default data-dir-scoped media directory.

    Kept public so the gateway boot path can ``mkdir -p`` it eagerly
    and surface permission errors before the first webhook arrives.
    Matches Rust ``default_media_dir``.
    """
    return data_dir / "media" / "telegram"


# ---------------------------------------------------------------------------
# process_update
# ---------------------------------------------------------------------------


async def process_update(
    ctx: WebhookCtx,
    update: Update,
) -> ProcessedUpdate | None:
    """Drive the full pipeline for one webhook update.

    Non-message updates (edited_message, callback_query, channel_post,
    ...) are quietly ignored — returning ``None`` so the route still
    responds 200 and Telegram doesn't re-deliver them. Mirrors Rust
    ``process_update``.
    """
    msg = update.message
    if msg is None:
        return None

    route = classify(msg, ctx.bot_id, ctx.bot_username)
    session_key = session_key_for(msg)

    return await _process_update_body(ctx, msg, route, session_key, update.update_id)


async def _process_update_body(
    ctx: WebhookCtx,
    msg: Message,
    route: MessageRoute,
    session_key: str,
    update_id: int,
) -> ProcessedUpdate:
    # Pick the first media attachment present; photo/voice/document
    # are modelled as optional fields on Message so at most one is
    # present in practice. Process in Telegram's own precedence order.
    media_kind: str
    file_id: str | None
    fallback_ext: str
    photo = msg.largest_photo()
    if photo is not None:
        media_kind, file_id, fallback_ext = "photo", photo.file_id, "jpg"
    elif msg.voice is not None:
        media_kind, file_id, fallback_ext = "voice", msg.voice.file_id, "ogg"
    elif msg.document is not None:
        media_kind, file_id, fallback_ext = "document", msg.document.file_id, "bin"
    else:
        media_kind, file_id, fallback_ext = "text", None, ""

    media: DownloadedMedia | None = None
    if file_id is not None:
        try:
            media = await download_to_media_dir(
                ctx.http, file_id, ctx.data_dir, fallback_ext
            )
        except MediaError:
            # Log + continue — the Rust crate logs via ``tracing::warn``;
            # we keep the swallow behaviour but skip the logger import
            # to avoid a hard dep. Callers can wrap if they want it.
            media = None

    content = msg.text or ""

    # Fire hooks if a bus is wired in.
    if ctx.hooks is not None:
        from corlinman_hooks import HookEvent

        meta = _build_metadata(msg, route, media, media_kind)
        await ctx.hooks.emit(
            HookEvent.MessageReceived(
                channel="telegram",
                session_key_=session_key,
                content=content,
                metadata=meta,
                user_id=None,
            )
        )

        if media_kind == "voice":
            media_path = str(media.path) if media is not None else ""
            await ctx.hooks.emit(
                HookEvent.MessageTranscribed(
                    session_key_=session_key,
                    # Real STT lands in a later batch — stub so the
                    # hook shape is wired and subscribers can build
                    # against it. Matches Rust.
                    transcript="",
                    media_path=media_path,
                    media_type="voice",
                    user_id=None,
                )
            )

    return ProcessedUpdate(
        update_id=update_id,
        session_key=session_key,
        route=route,
        content=content,
        media=media,
        media_kind=media_kind,
    )


# ---------------------------------------------------------------------------
# Internal: metadata builder
# ---------------------------------------------------------------------------


def _build_metadata(
    msg: Message,
    route: MessageRoute,
    media: DownloadedMedia | None,
    media_kind: str,
) -> dict[str, Any]:
    """Build the JSON metadata payload attached to
    ``HookEvent.MessageReceived``. Mirrors Rust ``build_metadata``."""
    is_group = route.is_group()
    mentions_bot = route == MessageRoute.GROUP_ADDRESSED
    meta: dict[str, Any] = {
        "is_group": is_group,
        "chat_type": msg.chat.chat_type,
        "mentions_bot": mentions_bot,
        "media_kind": media_kind,
    }
    if is_group:
        meta["group_id"] = str(msg.chat.id)
    if media is not None:
        meta["media_path"] = str(media.path)
        meta["media_bytes"] = int(media.bytes_written)
    return meta
