"""Telegram Bot HTTPS long-poll adapter.

Port of ``rust/.../telegram/`` — specifically ``service.rs`` (long-poll
driver), ``message.rs`` (wire types + mention helpers), and the
``types.rs`` classify-route helpers. Webhook handling is out of scope
for this iteration (the Rust webhook module is a parallel B4-BE1 deliverable
and the Python plane uses long-poll for now).

## Long-poll loop

1. ``GET /bot<token>/getMe`` once to discover the bot's id + username.
2. ``GET /bot<token>/getUpdates?offset=<last+1>&timeout=25`` in a loop.
3. Each :class:`Update` is decoded; non-message updates are dropped.
4. The ``offset`` is bumped past every ``update_id`` we see — even when
   the message is filtered out by allow-list / keyword rules — otherwise
   we'd reprocess the same updates forever.
5. Inbound messages are normalized into :class:`InboundEvent` objects.

## Shape

``async for event in adapter.inbound():`` yields normalized events; the
adapter handles the polling loop internally. A configurable HTTP transport
(:class:`httpx.AsyncClient` or any test double) lets the integration
tests substitute an in-process mock without touching the network.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from corlinman_channels.common import (
    ChannelBinding,
    ConfigError,
    InboundEvent,
    TransportError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Long-poll timeout passed to ``getUpdates``. Telegram recommends 25-50s;
#: we use 25 so the cancel→exit latency is bounded.
LONG_POLL_TIMEOUT: int = 25

#: Telegram bot-API hard limit for file downloads (and what ``sendVoice``
#: / ``sendPhoto`` accept). Documented at <https://core.telegram.org/bots/api>.
MAX_DOWNLOAD_BYTES: int = 20 * 1024 * 1024

#: Backoff after a ``getUpdates`` failure (seconds). Matches the 5s sleep
#: in ``rust/.../telegram/service.rs``.
ERROR_BACKOFF_SECS: float = 5.0


# ===========================================================================
# Wire types — pydantic so unknown fields default-decode cleanly.
# ===========================================================================


class User(BaseModel):
    """Telegram ``User`` subset."""

    model_config = ConfigDict(extra="ignore")

    id: int
    is_bot: bool = False
    username: str | None = None
    first_name: str | None = None


class Chat(BaseModel):
    """Telegram ``Chat`` subset.

    ``chat_type`` is the JSON key ``type`` renamed to avoid shadowing the
    Python builtin. ``is_private()`` matches ``Chat::is_private`` in Rust.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    chat_type: str = Field(alias="type")
    title: str | None = None
    username: str | None = None

    def is_private(self) -> bool:
        return self.chat_type == "private"


class PhotoSize(BaseModel):
    """One resolution variant of a Telegram photo upload."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str | None = None
    width: int = 0
    height: int = 0
    file_size: int | None = None


class Voice(BaseModel):
    """Telegram voice note (OGG/OPUS)."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str | None = None
    duration: int = 0
    mime_type: str | None = None
    file_size: int | None = None


class Document(BaseModel):
    """Generic Telegram document attachment."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class MessageEntity(BaseModel):
    """One ``MessageEntity`` — only mention-related types are interpreted."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    entity_type: str = Field(alias="type")
    offset: int = 0
    length: int = 0
    user: User | None = None


class Message(BaseModel):
    """Telegram ``Message`` subset.

    Subset matches the Rust ``Message`` struct field-for-field; richer
    fields (video / sticker / edit_date) deserialise as ignored defaults.
    """

    model_config = ConfigDict(extra="ignore")

    message_id: int
    from_: User | None = Field(default=None, alias="from")
    chat: Chat
    date: int
    text: str | None = None
    entities: list[MessageEntity] = Field(default_factory=list)
    reply_to_message: Message | None = None
    photo: list[PhotoSize] = Field(default_factory=list)
    voice: Voice | None = None
    document: Document | None = None

    def largest_photo(self) -> PhotoSize | None:
        """Pick the largest :class:`PhotoSize` (max by ``file_size``)."""
        if not self.photo:
            return None
        with_size = [p for p in self.photo if p.file_size is not None]
        if with_size:
            return max(with_size, key=lambda p: p.file_size or 0)
        return self.photo[-1]


# pydantic forward-ref resolution for the self-referential reply_to_message.
Message.model_rebuild()


class Update(BaseModel):
    """One item from ``getUpdates``."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: Message | None = None


class File(BaseModel):
    """Result of ``GET /bot<token>/getFile``."""

    model_config = ConfigDict(extra="ignore")

    file_id: str = ""
    file_unique_id: str | None = None
    file_size: int | None = None
    file_path: str | None = None


# ===========================================================================
# Mention / route helpers — match ``message.rs`` + ``types.rs``.
# ===========================================================================


class MessageRoute(StrEnum):
    """How an inbound message should be routed.

    Mirrors the Rust ``MessageRoute`` enum used by the webhook handler.
    Long-poll mode doesn't *need* this distinction internally (the loop
    just decides whether to dispatch) but it's exported here so consumers
    can build their own gating without rewriting the logic.
    """

    PRIVATE = "private"
    """Private 1:1 DM. Always respond."""

    GROUP_ADDRESSED = "group_addressed"
    """Group / supergroup where the bot is addressed. Respond."""

    GROUP_IGNORED = "group_ignored"
    """Group where the bot is not addressed. Emit-only."""

    def should_respond(self) -> bool:
        return self in (MessageRoute.PRIVATE, MessageRoute.GROUP_ADDRESSED)

    def is_group(self) -> bool:
        return self in (MessageRoute.GROUP_ADDRESSED, MessageRoute.GROUP_IGNORED)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice ``text`` in UTF-16 code units (Telegram's entity offsets).

    Necessary because Telegram counts entity offsets in UTF-16 — for
    Chinese / emoji messages bytes != chars != utf16 units. Matches the
    Rust ``utf16_slice`` helper in ``message.rs``.
    """
    if offset < 0 or length < 0:
        return ""
    units = text.encode("utf-16-le")
    start = offset * 2
    end = min(start + length * 2, len(units))
    if start >= len(units):
        return ""
    return units[start:end].decode("utf-16-le", errors="replace")


def is_mentioning_bot(
    msg: Message,
    bot_id: int,
    bot_username: str | None,
) -> bool:
    """True iff the message mentions the bot (by id or by username).

    Matches ``is_mentioning_bot`` in Rust ``message.rs``. Both entity
    forms are considered: ``text_mention`` with ``user.id == bot_id`` and
    ``mention`` whose sliced text equals ``@<bot_username>``.
    """
    text = msg.text
    if text is None:
        return False
    for entity in msg.entities:
        if entity.entity_type == "text_mention":
            if entity.user is not None and entity.user.id == bot_id:
                return True
        elif entity.entity_type == "mention" and bot_username is not None:
            slice_ = _utf16_slice(text, entity.offset, entity.length)
            expected = f"@{bot_username}"
            if slice_.lower() == expected.lower():
                return True
    return False


def classify(msg: Message, bot_id: int, bot_username: str | None) -> MessageRoute:
    """Decide routing for one inbound :class:`Message`.

    Pure function — keeps the loop / webhook handlers thin and the unit
    tests trivial.
    """
    if msg.chat.is_private():
        return MessageRoute.PRIVATE

    if is_mentioning_bot(msg, bot_id, bot_username):
        return MessageRoute.GROUP_ADDRESSED

    # Substring fallback for forwarded messages that stripped entities.
    if msg.text is not None and bot_username is not None:
        needle = f"@{bot_username}".lower()
        if needle in msg.text.lower():
            return MessageRoute.GROUP_ADDRESSED

    # Reply-to-bot fallback.
    if msg.reply_to_message is not None and msg.reply_to_message.from_ is not None:
        from_ = msg.reply_to_message.from_
        if from_.id == bot_id:
            return MessageRoute.GROUP_ADDRESSED
        if (
            bot_username is not None
            and from_.username is not None
            and from_.username.lower() == bot_username.lower()
        ):
            return MessageRoute.GROUP_ADDRESSED

    return MessageRoute.GROUP_IGNORED


def binding_from_message(msg: Message, bot_id: int) -> ChannelBinding:
    """Build a :class:`ChannelBinding` from a Telegram message.

    Matches ``binding_from_message`` in Rust ``message.rs``. Private
    chats: ``thread == sender == chat.id == user.id``. Groups:
    ``thread == chat.id``, ``sender == from.id``.
    """
    sender = msg.from_.id if msg.from_ is not None else msg.chat.id
    return ChannelBinding.telegram(bot_id=bot_id, chat_id=msg.chat.id, user_id=sender)


def session_key_for(msg: Message) -> str:
    """Return the conversation session key for ``msg``.

    Private chats: ``telegram:<chat_id>:<user_id>``; group chats:
    ``telegram:<chat_id>:group``. Matches ``session_key_for`` in Rust
    ``types.rs``.
    """
    if msg.chat.is_private():
        user_id = msg.from_.id if msg.from_ is not None else msg.chat.id
        return f"telegram:{msg.chat.id}:{user_id}"
    return f"telegram:{msg.chat.id}:group"


# ===========================================================================
# Adapter
# ===========================================================================


@dataclass(slots=True)
class TelegramConfig:
    """Configuration for :class:`TelegramAdapter`.

    ``bot_token`` is required. ``allowed_chat_ids`` (empty == allow all)
    and ``keyword_filter`` (case-insensitive substring; empty == allow
    all) match the gates in Rust ``service.rs``. ``require_mention_in_groups``
    short-circuits unaddressed group messages before keyword matching.
    """

    bot_token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    keyword_filter: list[str] = field(default_factory=list)
    require_mention_in_groups: bool = False
    base_url: str = "https://api.telegram.org"
    long_poll_timeout: int = LONG_POLL_TIMEOUT


class TelegramAdapter:
    """HTTPS long-poll Telegram Bot adapter.

    Same surface as the other adapters: ``async with`` for lifecycle,
    ``inbound()`` for the normalized event stream.

    The adapter doesn't implement outbound ``sendMessage`` here — that's
    a separate concern and the Rust crate keeps it in its own ``send.rs``
    module. A future port can layer a sender on top without changing this
    inbound shape.
    """

    def __init__(
        self,
        config: TelegramConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.bot_token:
            raise ConfigError("TelegramConfig.bot_token is empty")
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.long_poll_timeout + 5)
        )
        self._closed = False
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._inbound_q: asyncio.Queue[Message] = asyncio.Queue(maxsize=256)
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TelegramAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Resolve the bot id/username and spawn the long-poll loop.

        ``getMe`` must succeed before polling begins — otherwise we have
        no way to compute the mention/route gates. A network failure
        here raises :class:`TransportError` so the caller fails fast.
        """
        if self._reader_task is not None:
            return
        me = await self._get_me()
        self._bot_id = me.id
        self._bot_username = me.username
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._poll_loop(), name="telegram-poll"
        )

    async def close(self) -> None:
        """Stop the long-poll loop and (if we own it) the HTTP client."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def bot_id(self) -> int | None:
        """The bot's numeric id, available after :meth:`connect`."""
        return self._bot_id

    @property
    def bot_username(self) -> str | None:
        """The bot's ``@username`` (without the ``@``), if any."""
        return self._bot_username

    # ------------------------------------------------------------------
    # Inbound iterator
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[InboundEvent[Message]]:
        """Yield one :class:`InboundEvent` per accepted inbound message.

        Filtering rules (matching Rust ``service.rs``):
        - ``allowed_chat_ids`` whitelist (empty = allow all).
        - In groups: optional require_mention + keyword filter.
        - Empty / whitespace-only text is silently skipped.
        """
        if self._reader_task is None:
            await self.connect()
        assert self._bot_id is not None  # connect() guarantees this
        bot_id = self._bot_id
        bot_username = self._bot_username
        while not self._closed:
            try:
                msg = await self._inbound_q.get()
            except asyncio.CancelledError:
                return

            if not self._chat_allowed(msg.chat.id):
                continue

            mentioned = self._is_bot_addressed(msg)

            if not msg.chat.is_private():
                if self._cfg.require_mention_in_groups and not mentioned:
                    continue
                if not mentioned and not self._keyword_match(msg):
                    continue

            text = msg.text
            if text is None or not text.strip():
                continue

            binding = binding_from_message(msg, bot_id)
            yield InboundEvent(
                channel="telegram",
                binding=binding,
                text=text,
                message_id=str(msg.message_id),
                timestamp=msg.date,
                mentioned=mentioned or msg.chat.is_private(),
                attachments=[],  # download is a follow-up step — out of scope here
                payload=msg,
            )
            # Note `bot_username` is unused inside the loop body but kept in
            # scope so future refactors that need it don't re-read it from
            # self (which would race with adapter reconfiguration).
            _ = bot_username

    # ------------------------------------------------------------------
    # Long-poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        offset: int | None = None
        while not self._closed:
            try:
                updates = await self._get_updates(offset)
            except asyncio.CancelledError:
                return
            except Exception:
                # Transient — back off and retry, matching Rust's 5s sleep.
                try:
                    await asyncio.sleep(ERROR_BACKOFF_SECS)
                except asyncio.CancelledError:
                    return
                continue

            # Bail out before touching the queue if the consumer asked to
            # stop while we were in flight (``close()`` flipped the flag
            # while we awaited the HTTP response).
            if self._closed:
                return

            for upd in updates:
                # Advance offset regardless of filter outcome — the Rust
                # service does the same so dropped updates don't replay.
                offset = upd.update_id + 1
                if upd.message is None:
                    continue
                if self._closed:
                    return
                try:
                    await self._inbound_q.put(upd.message)
                except asyncio.CancelledError:
                    return

            # Yield unconditionally — when the transport completes
            # instantly (``httpx.MockTransport`` in tests, or a Telegram
            # edge that returns an empty long-poll batch with no real
            # network wait) the ``await`` chain inside ``_get_updates``
            # may resolve without ever surrendering the event loop,
            # starving the inbound consumer. ``sleep(0)`` gives the
            # consumer a turn to drain a freshly enqueued message and to
            # observe ``close()``. Real Telegram long polls block for
            # tens of seconds so the cost in production is nil.
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------
    # HTTP surface
    # ------------------------------------------------------------------

    def _endpoint(self, method: str) -> str:
        return f"{self._cfg.base_url}/bot{self._cfg.bot_token}/{method}"

    async def _get_me(self) -> User:
        resp = await self._client.get(self._endpoint("getMe"))
        env = _unwrap(resp)
        if not isinstance(env, dict):
            raise TransportError("getMe returned non-object result")
        return User.model_validate(env)

    async def _get_updates(self, offset: int | None) -> list[Update]:
        params: dict[str, Any] = {"timeout": self._cfg.long_poll_timeout}
        if offset is not None:
            params["offset"] = offset
        resp = await self._client.get(self._endpoint("getUpdates"), params=params)
        env = _unwrap(resp)
        if env is None:
            return []
        if not isinstance(env, list):
            raise TransportError("getUpdates returned non-array result")
        return [Update.model_validate(u) for u in env if isinstance(u, dict)]

    # ------------------------------------------------------------------
    # Gates — match service.rs helpers
    # ------------------------------------------------------------------

    def _chat_allowed(self, chat_id: int) -> bool:
        allow = self._cfg.allowed_chat_ids
        return not allow or chat_id in allow

    def _keyword_match(self, msg: Message) -> bool:
        filter_ = self._cfg.keyword_filter
        if not filter_:
            return True
        text = msg.text
        if text is None:
            return False
        lower = text.lower()
        return any(kw.lower() in lower for kw in filter_)

    def _is_bot_addressed(self, msg: Message) -> bool:
        """Entity mention OR reply-to-bot. Combines ``is_mentioning_bot``
        with the reply-id fallback from ``service.rs::is_bot_addressed``."""
        assert self._bot_id is not None
        if is_mentioning_bot(msg, self._bot_id, self._bot_username):
            return True
        reply = msg.reply_to_message
        if reply is not None and reply.from_ is not None:
            if reply.from_.id == self._bot_id and reply.from_.is_bot:
                return True
        return False


# ===========================================================================
# Helpers
# ===========================================================================


def _unwrap(resp: httpx.Response) -> Any:
    """Lift the Telegram envelope ``{ok, result, description?}``.

    Raises :class:`TransportError` on HTTP errors and on ``ok=False`` API
    responses (mirrors the Rust ``TgEnvelope::into_result`` behaviour).
    """
    if resp.status_code >= 400:
        raise TransportError(f"telegram HTTP {resp.status_code}: {resp.text}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise TransportError(f"telegram invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise TransportError("telegram response was not a JSON object")
    if not body.get("ok"):
        raise TransportError(
            f"telegram api error: {body.get('description', '<no description>')}"
        )
    return body.get("result")


_MENTION_RE = re.compile(r"@(\w+)")


def extract_mentions(text: str) -> list[str]:
    """Pull bare ``@username`` mentions out of plain text.

    Used by tests and by callers that want a quick mention check without
    parsing entities. Returns lowercase usernames so comparison is
    case-insensitive.
    """
    return [m.group(1).lower() for m in _MENTION_RE.finditer(text)]


__all__ = [
    "ERROR_BACKOFF_SECS",
    "LONG_POLL_TIMEOUT",
    "MAX_DOWNLOAD_BYTES",
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
    "extract_mentions",
    "is_mentioning_bot",
    "session_key_for",
]


# ---------------------------------------------------------------------------
# Compatibility for callers expecting `sequence` typing.
# ---------------------------------------------------------------------------

# Re-export Sequence so callers can spell allow-list parameters in their
# own functions without an extra import; not in __all__ since it's just
# a convenience alias.
_Sequence = Sequence
