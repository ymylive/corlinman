"""Channel dispatcher — ``MessageEvent`` → keyword filter → rate-limit
→ normalized request.

Python port of ``rust/.../router.rs``. The router is the first point
inside corlinman that has an opinion about *whether* to respond. It
reads:

- ``QQ_GROUP_KEYWORDS`` (JSON map) to decide which group messages
  qualify,
- the bot's ``self_id`` list so ``@mention`` triggers bypass keyword
  filtering,
- optional per-group / per-sender token buckets
  (:class:`corlinman_channels.rate_limit.TokenBucket`) so runaway
  keyword hits don't blast the backend.

The Rust crate emits a typed ``ChatRequest``; the Python plane doesn't
have a ported chat-request struct yet, so :meth:`ChannelRouter.dispatch`
returns a lightweight :class:`RoutedRequest` dataclass with the same
fields (content / binding / message_id / timestamp / mentioned /
session_key). Downstream code can lift this into whatever request
type the gateway-side Python service ends up adopting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from corlinman_channels.common import ChannelBinding
from corlinman_channels.onebot import (
    AtSegment,
    MessageEvent,
    MessageType,
    is_mentioned,
    segments_to_text,
)
from corlinman_channels.rate_limit import TokenBucket

__all__ = [
    "ChannelRouter",
    "GroupKeywords",
    "RateLimitHook",
    "RoutedRequest",
    "parse_group_keywords",
]


# ---------------------------------------------------------------------------
# Public type aliases / protocols
# ---------------------------------------------------------------------------


#: ``QQ_GROUP_KEYWORDS`` JSON schema is
#: ``{"<group_id>": ["kw1", "kw2"], ...}``. Group ids are stringified
#: because JSON object keys must be strings; values are case-insensitive
#: substring matches against the flattened message text. Groups absent
#: from the map default to "dispatch every message".
GroupKeywords = dict[str, list[str]]


class RateLimitHook(Protocol):
    """Callback invoked whenever a message is silently dropped by a
    rate-limit check.

    Mirrors the Rust ``RateLimitHook`` type alias. The gateway wires
    this to a Prometheus CounterVec
    (``corlinman_channels_rate_limited_total{channel, reason}``);
    tests pass a closure that tallies calls.

    Must be cheap — runs on the hot path inline with :meth:`ChannelRouter.dispatch`.
    Two positional labels: ``(channel, reason)``.
    """

    def __call__(self, channel: str, reason: str) -> None:
        ...


# ---------------------------------------------------------------------------
# parse_group_keywords
# ---------------------------------------------------------------------------


def parse_group_keywords(raw: str) -> GroupKeywords:
    """Parse ``QQ_GROUP_KEYWORDS`` env var (JSON).

    Missing / empty env returns an empty map — dispatch-all for every
    group. Matches ``parse_group_keywords`` in Rust ``router.rs``.

    Raises :class:`json.JSONDecodeError` on malformed input (the Rust
    counterpart returns ``serde_json::Error``; Python keeps the
    exception form so callers can catch by stdlib type).
    """
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        # Match Rust semantics: a non-object payload is a decode error.
        raise json.JSONDecodeError(
            "expected JSON object at top level", raw, 0
        )
    # Coerce keys to str and values to list[str] defensively; Telegram
    # / NapCat occasionally ship integer keys after a TOML conversion.
    out: GroupKeywords = {}
    for k, v in parsed.items():
        if not isinstance(v, list):
            raise json.JSONDecodeError(
                f"value for key {k!r} must be an array", raw, 0
            )
        out[str(k)] = [str(item) for item in v]
    return out


# ---------------------------------------------------------------------------
# RoutedRequest — the Python analog of the Rust ChatRequest
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RoutedRequest:
    """Lightweight ``ChatRequest`` analog returned by
    :meth:`ChannelRouter.dispatch`.

    The Rust crate emits a fully-typed ``ChatRequest`` from
    ``corlinman-core``. The Python plane doesn't have a port of that
    struct yet, so we shape the fields one-for-one and let downstream
    callers lift this into whatever request type the eventual Python
    gateway adopts.
    """

    binding: ChannelBinding
    content: str
    message_id: str | None = None
    timestamp: int = 0
    mentioned: bool = False

    @property
    def session_key(self) -> str:
        """Forward to :meth:`ChannelBinding.session_key` so callers
        can write ``req.session_key`` without an extra hop."""
        return self.binding.session_key()


# ---------------------------------------------------------------------------
# ChannelRouter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelRouter:
    """Router state — cheap to share across spawned tasks.

    Mirrors the Rust ``ChannelRouter`` struct field-for-field; the
    builder methods mirror the Rust ``with_*`` helpers.
    """

    group_keywords: GroupKeywords = field(default_factory=dict)
    """Per-group keyword filter (case-insensitive substring match)."""

    self_ids: list[int] = field(default_factory=list)
    """``@mention`` targets that always trigger, independent of
    keywords. In OneBot this is the bot's own ``self_id``."""

    group_limiter: TokenBucket | None = None
    """Optional per-group token bucket. ``None`` ⇒ dimension disabled.
    Keyed by ``"<channel>:<thread>"``."""

    sender_limiter: TokenBucket | None = None
    """Optional per-sender token bucket. Keyed by
    ``"<channel>:<thread>:<sender>"``."""

    rate_limit_hook: RateLimitHook | None = None
    """Observation hook fired on every silent drop due to a rate-limit
    check. Wired to Prometheus in production; ``None`` in tests."""

    hook_bus: Any = None
    """Optional :class:`corlinman_hooks.HookBus`. When set, every
    rate-limit rejection is additionally mirrored to
    ``HookEvent.RateLimitTriggered`` so cross-component subscribers
    observe drops. Additive: the legacy ``rate_limit_hook`` callback
    still fires. Typed ``Any`` to avoid a hard import dependency on
    corlinman-hooks at module load time (tests assert on the event
    shape and import the bus directly)."""

    # ------------------------------------------------------------------
    # Builders — mirror Rust ``with_*`` ergonomics.
    # ------------------------------------------------------------------

    def with_rate_limits(
        self,
        group: TokenBucket | None,
        sender: TokenBucket | None,
    ) -> ChannelRouter:
        """Attach per-group and per-sender token buckets. Either may
        be ``None`` to leave that dimension disabled. Returns ``self``
        for chaining (matches Rust ``with_rate_limits``)."""
        self.group_limiter = group
        self.sender_limiter = sender
        return self

    def with_rate_limit_hook(self, hook: RateLimitHook) -> ChannelRouter:
        """Attach a drop-observation hook (typically a Prometheus
        counter increment)."""
        self.rate_limit_hook = hook
        return self

    def with_hook_bus(self, bus: Any) -> ChannelRouter:
        """Attach the unified hook bus. When set, every rate-limit
        drop additionally emits a ``HookEvent.RateLimitTriggered`` on
        the bus. Additive — the legacy callback still fires."""
        self.hook_bus = bus
        return self

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, event: MessageEvent) -> RoutedRequest | None:
        """Apply the keyword/mention gate + rate-limit checks and
        return a :class:`RoutedRequest` if the message should be
        forwarded.

        Returns ``None`` when the message is filtered out (heartbeat,
        wrong message_type, keyword mismatch, empty body, rate-limited,
        ...). All drops are silent — callers log at DEBUG if they want
        visibility, and rate-limit drops additionally fire
        :attr:`rate_limit_hook`.

        Mirrors ``ChannelRouter::dispatch`` in Rust step-for-step.
        """
        text = _flatten_and_trim(event.message, event.raw_message)

        # @mention short-circuits keyword filtering. Matches qqBot.js
        # line 298-336 / Rust router lines ~150.
        mentioned = any(is_mentioned(event.message, sid) for sid in self.self_ids)

        if event.message_type == MessageType.PRIVATE:
            binding = ChannelBinding.qq_private(event.self_id, event.user_id)
        elif event.message_type == MessageType.GROUP:
            group_id = event.group_id
            if group_id is None:
                return None
            if not mentioned and not self._keyword_match(group_id, text):
                return None
            binding = ChannelBinding.qq_group(event.self_id, group_id, event.user_id)
        else:
            # Unreachable — MessageType is a closed enum, but be defensive.
            return None

        # Drop completely empty messages (pure sticker / pure recall placeholder).
        if not text.strip():
            return None

        # Rate-limit gates run AFTER keyword/mention so that dropped
        # messages never consume tokens. Per-group first (cheaper,
        # smaller cardinality).
        if self.group_limiter is not None:
            key = f"{binding.channel}:{binding.thread}"
            if not self.group_limiter.check(key):
                self._fire_hook(binding.channel, "group")
                self._emit_bus_rate_limit(binding, "group")
                return None
        if self.sender_limiter is not None:
            key = f"{binding.channel}:{binding.thread}:{binding.sender}"
            if not self.sender_limiter.check(key):
                self._fire_hook(binding.channel, "sender")
                self._emit_bus_rate_limit(binding, "sender")
                return None

        return RoutedRequest(
            binding=binding,
            content=text,
            message_id=str(event.message_id),
            timestamp=event.time,
            mentioned=mentioned,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_hook(self, channel: str, reason: str) -> None:
        if self.rate_limit_hook is not None:
            self.rate_limit_hook(channel, reason)

    def _emit_bus_rate_limit(self, binding: ChannelBinding, reason: str) -> None:
        """Bus mirror for rate-limit rejections.

        ``limit_type`` is rendered as ``"<reason>_<channel>"`` (e.g.
        ``"group_qq"``) so a single bus subscriber can discriminate
        both dimensions without parsing the callback tuple. No-op when
        no bus is attached. Matches Rust ``emit_bus_rate_limit``.
        """
        if self.hook_bus is None:
            return
        # Lazy-import to keep corlinman-hooks an optional / soft dep
        # (the import succeeds inside the corlinman workspace venv;
        # standalone publishes can opt out).
        from corlinman_hooks import HookEvent

        limit_type = f"{reason}_{binding.channel}"
        ev = HookEvent.RateLimitTriggered(
            session_key_=binding.session_key(),
            limit_type=limit_type,
            retry_after_ms=0,
            user_id=None,
        )
        # The bus's ``emit_nonblocking`` is the right shape for a
        # synchronous hot-path emission (matches Rust ``bus.emit_nonblocking``).
        self.hook_bus.emit_nonblocking(ev)

    def _keyword_match(self, group_id: int, text: str) -> bool:
        kws = self.group_keywords.get(str(group_id))
        if kws is None:
            # No keyword list configured → dispatch-all (default).
            return True
        if not kws:
            return True
        lower = text.lower()
        return any(kw.lower() in lower for kw in kws)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _flatten_and_trim(
    segments: Iterable[Any],
    raw: str,
) -> str:
    """Prefer the OneBot-supplied ``raw_message`` (already CQ-flattened)
    when present; otherwise re-extract from segments. Matches Rust
    ``flatten_and_trim`` in ``router.rs``.

    Note: ``AtSegment`` participation in the fallback is unchanged
    from :func:`corlinman_channels.onebot.segments_to_text` —
    @mentions are surfaced as ``@<qq> `` so keyword routing still sees
    the address.
    """
    if raw:
        return raw
    # Keep the AtSegment import referenced so static analysers don't
    # flag the symbol as unused — the `segments_to_text` helper drives
    # the actual flattening but importing the segment type at module
    # scope keeps the contract surface obvious from the imports list.
    _ = AtSegment
    return segments_to_text(segments)
