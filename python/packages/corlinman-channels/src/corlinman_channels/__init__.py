"""corlinman-channels — inbound channel adapters (Python plane).

Python port of the Rust ``corlinman-channels`` crate. Three inbound
transports share one uniform shape:

* :class:`OneBotAdapter` — QQ via gocq / NapCat over a forward-WebSocket
  using the OneBot v11 protocol.
* :class:`LogStreamAdapter` — WebSocket subscriber for structured log
  frames (newline-delimited JSON, one frame per WS text frame).
* :class:`TelegramAdapter` — Telegram Bot API HTTPS ``getUpdates``
  long-poll.

Each adapter exposes ``async for event in adapter.inbound(): ...`` and
yields the same :class:`InboundEvent` envelope so consumers don't need
to special-case the transport.

Plus the cross-cutting machinery the gateway wires on top:

* :class:`ChannelRegistry` / :class:`ChannelContext` / :func:`spawn_all`
  — the uniform Channel Protocol the gateway iterates over.
* :class:`ChannelRouter` — keyword / @mention gate + rate-limit hooks
  for the OneBot dispatcher.
* :class:`TokenBucket` — per-key token-bucket rate limiter.
* :class:`TelegramSender` / :class:`TelegramHttp` / :func:`process_update`
  — the Telegram outbound + webhook surface.
* :func:`run_qq_channel` / :func:`run_telegram_channel` — orchestration
  helpers wiring an adapter to a chat backend (parallel to Rust
  ``service.rs``).

The W1 :class:`UserId` is re-exported here for convenience; an adapter
that has access to an identity store can populate
``InboundEvent.user_id`` to bridge per-channel ids to a canonical
opaque handle.
"""

from corlinman_channels.channel import (
    ApnsChannel,
    Channel,
    ChannelContext,
    ChannelError,
    ChannelRegistry,
    QqChannel,
    TelegramChannel,
    spawn_all,
)
from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundAdapter,
    InboundEvent,
    TransportError,
    UnsupportedError,
    UserId,
)
from corlinman_channels.common import ChannelError as _CommonChannelError
from corlinman_channels.logstream import (
    LogFrame,
    LogStreamAdapter,
    LogStreamConfig,
)
from corlinman_channels.onebot import (
    Action,
    AtSegment,
    Event,
    FaceSegment,
    ForwardNode,
    ForwardSegment,
    ImageSegment,
    MessageEvent,
    MessageSegment,
    MessageType,
    MetaEvent,
    NoticeEvent,
    OneBotAdapter,
    OneBotConfig,
    OtherSegment,
    RecordSegment,
    ReplySegment,
    RequestEvent,
    Sender,
    SendGroupForwardMsg,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
    UnknownEvent,
    action_to_wire,
    is_mentioned,
    parse_event,
    segments_to_attachments,
    segments_to_text,
)
from corlinman_channels.rate_limit import (
    GC_INTERVAL,
    GC_STALE_AFTER,
    TokenBucket,
)
from corlinman_channels.router import (
    ChannelRouter,
    GroupKeywords,
    RateLimitHook,
    RoutedRequest,
    parse_group_keywords,
)
from corlinman_channels.service import (
    ChatEventLike,
    ChatServiceLike,
    QqChannelParams,
    TelegramChannelParams,
    handle_one_qq,
    handle_one_telegram,
    run_qq_channel,
    run_telegram_channel,
)
from corlinman_channels.telegram import (
    Chat,
    Document,
    File,
    Message,
    MessageEntity,
    MessageRoute,
    PhotoSize,
    TelegramAdapter,
    TelegramConfig,
    Update,
    User,
    Voice,
    binding_from_message,
    classify,
    is_mentioning_bot,
    session_key_for,
)
from corlinman_channels.telegram_media import (
    DownloadedMedia,
    HttpxTelegramHttp,
    MediaError,
    TelegramHttp,
    download_to_media_dir,
)
from corlinman_channels.telegram_send import (
    PhotoSource,
    SendError,
    TelegramSender,
)
from corlinman_channels.telegram_webhook import (
    ProcessedUpdate,
    WebhookContext,
    WebhookCtx,
    WebhookError,
    default_media_dir,
    process_update,
    verify_secret,
)

# ``ChannelError`` is defined in *both* ``common`` (the base error for
# adapter operations) and ``channel`` (the trait-surface error factory).
# Keep the channel-side name as the public ``ChannelError`` because the
# Rust crate's external API surface matches that one; the common base
# remains accessible via ``corlinman_channels.common.ChannelError`` for
# subclassing.
_ = _CommonChannelError

__all__ = [  # noqa: RUF022 — grouped by subsystem for human readability.
    # Common / shared
    "Attachment",
    "AttachmentKind",
    "ChannelBinding",
    "ConfigError",
    "InboundAdapter",
    "InboundEvent",
    "TransportError",
    "UnsupportedError",
    "UserId",
    # Channel registry surface
    "ApnsChannel",
    "Channel",
    "ChannelContext",
    "ChannelError",
    "ChannelRegistry",
    "QqChannel",
    "TelegramChannel",
    "spawn_all",
    # Rate limit
    "GC_INTERVAL",
    "GC_STALE_AFTER",
    "TokenBucket",
    # Router
    "ChannelRouter",
    "GroupKeywords",
    "RateLimitHook",
    "RoutedRequest",
    "parse_group_keywords",
    # Service orchestration
    "ChatEventLike",
    "ChatServiceLike",
    "QqChannelParams",
    "TelegramChannelParams",
    "handle_one_qq",
    "handle_one_telegram",
    "run_qq_channel",
    "run_telegram_channel",
    # OneBot
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
    "Sender",
    "SendGroupForwardMsg",
    "SendGroupMsg",
    "SendPrivateMsg",
    "TextSegment",
    "UnknownEvent",
    "action_to_wire",
    "is_mentioned",
    "parse_event",
    "segments_to_attachments",
    "segments_to_text",
    # LogStream
    "LogFrame",
    "LogStreamAdapter",
    "LogStreamConfig",
    # Telegram inbound + parsing
    "Chat",
    "Document",
    "File",
    "Message",
    "MessageEntity",
    "MessageRoute",
    "PhotoSize",
    "TelegramAdapter",
    "TelegramConfig",
    "Update",
    "User",
    "Voice",
    "binding_from_message",
    "classify",
    "is_mentioning_bot",
    "session_key_for",
    # Telegram outbound (media + send + webhook)
    "DownloadedMedia",
    "HttpxTelegramHttp",
    "MediaError",
    "PhotoSource",
    "ProcessedUpdate",
    "SendError",
    "TelegramHttp",
    "TelegramSender",
    "WebhookContext",
    "WebhookCtx",
    "WebhookError",
    "default_media_dir",
    "download_to_media_dir",
    "process_update",
    "verify_secret",
]
