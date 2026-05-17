"""Channel adapter Protocol.

The Rust crate keeps the channel name as an opaque ``&str`` because the
identity DB only ever stores it as TEXT. Python-side gateway code wants
a richer surface — adapters that know how to render a verification
phrase back out to their channel, surface a display name hint, and
identify themselves with a stable slug.

This module defines :class:`ChannelAdapter`, a structural Protocol that
the chat plugins (QQ, Telegram, iOS, ...) can implement to plug into
:class:`UserIdentityResolver`'s high-level workflow surface. The
plain-string ``channel`` parameter on :class:`SqliteIdentityStore`
methods keeps working unchanged — adapters layer on top, they don't
replace.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from corlinman_identity.types import VerificationPhrase


@runtime_checkable
class ChannelAdapter(Protocol):
    """One channel's plug-in surface.

    Adapters are small and stateless; the heavy lifting lives in the
    resolver. The Protocol intentionally captures only what the
    resolver needs:

    * a stable :meth:`name` that becomes the SQLite ``channel`` column
      value (``"qq"``, ``"telegram"``, ``"ios"``);
    * an optional :meth:`echo_phrase` that hands a freshly-issued
      :class:`VerificationPhrase` back to the human on the source
      channel — adapters that can't push messages (e.g. a one-way
      webhook receiver) can implement this as a no-op and rely on
      operator manual delivery.
    """

    def name(self) -> str:
        """Stable channel slug. Stored verbatim in the SQLite ``channel``
        column so adapter renames break alias resolution — pick once."""
        ...

    async def echo_phrase(
        self,
        channel_user_id: str,
        phrase: VerificationPhrase,
    ) -> None:
        """Push the freshly-issued ``phrase`` to ``channel_user_id`` on
        the adapter's channel.

        Should be best-effort and idempotent — the operator can always
        re-issue if delivery fails.
        """
        ...


class ChannelRegistry:
    """In-memory registry mapping channel slugs to adapters.

    Mainly a convenience for the gateway boot path. The identity store
    itself doesn't need a registry — it works with bare channel names
    — but the resolver workflow (issue → echo → redeem) uses one so
    ``UserIdentityResolver.issue_and_echo`` can pick the right adapter.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        """Add ``adapter`` to the registry, keyed by its ``name()``.

        Re-registering the same name replaces the previous adapter
        (matches the gateway hot-reload story). Adapters without a
        stable name() value are rejected — that would silently break
        alias resolution.
        """
        slug = adapter.name()
        if not slug:
            raise ValueError("channel adapter must return a non-empty name()")
        self._adapters[slug] = adapter

    def get(self, channel: str) -> ChannelAdapter | None:
        """Return the adapter for ``channel``, or ``None`` if missing."""
        return self._adapters.get(channel)

    def names(self) -> list[str]:
        """List registered channel slugs in insertion order."""
        return list(self._adapters)


__all__ = [
    "ChannelAdapter",
    "ChannelRegistry",
]
