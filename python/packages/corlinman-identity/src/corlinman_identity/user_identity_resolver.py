""":class:`UserIdentityResolver` — high-level facade over the store.

Wraps a :class:`SqliteIdentityStore` and an optional
:class:`ChannelRegistry` to expose the workflow the gateway middleware
actually wants:

* ``resolve(channel, channel_user_id, *, display_name_hint=None)`` —
  resolve-or-create, same semantics as the underlying store.
* ``link(into_user_id, from_user_id, *, decided_by)`` — operator-driven
  merge.
* ``verify_issue(user_id, channel, channel_user_id)`` — issue + (when a
  channel adapter is registered) echo the phrase to the human.
* ``verify_redeem(phrase, channel, channel_user_id)`` — the human's
  paste-side handler.

The method names ``resolve`` / ``link`` / ``verify`` match the
porting brief ("async resolve/link/verify methods (1:1 with Rust
signature)"). Underlying SQL + transactional behaviour comes from
:class:`SqliteIdentityStore`, so the wire shapes stay byte-identical
across the Rust and Python implementations.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_identity.channels import ChannelRegistry
from corlinman_identity.store import SqliteIdentityStore, identity_db_path
from corlinman_identity.tenancy import TenantIdLike
from corlinman_identity.types import (
    ChannelAlias,
    UserId,
    UserSummary,
    VerificationPhrase,
)

# Importing :mod:`verification` for its side effect — registers
# ``issue_phrase`` / ``redeem_phrase`` / ``sweep_expired_phrases`` on
# :class:`SqliteIdentityStore`. The import is load-bearing; do not
# remove even though no symbol is consumed here.
from corlinman_identity import verification as _verification  # noqa: F401

# Same story for the resolver methods.
from corlinman_identity import resolver as _resolver  # noqa: F401


class UserIdentityResolver:
    """High-level resolver facade.

    Composes a :class:`SqliteIdentityStore` (the persistence layer)
    with an optional :class:`ChannelRegistry` (the egress layer). All
    multi-step verification flows route through this class; per-tenant
    isolation comes from the store's underlying DB path.
    """

    def __init__(
        self,
        store: SqliteIdentityStore,
        *,
        channels: ChannelRegistry | None = None,
    ) -> None:
        self._store = store
        self._channels = channels or ChannelRegistry()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        data_dir: Path,
        tenant: TenantIdLike | str,
        *,
        channels: ChannelRegistry | None = None,
    ) -> UserIdentityResolver:
        """Open the per-tenant identity DB under ``data_dir``.

        Convenience constructor — equivalent to
        ``SqliteIdentityStore.open(identity_db_path(data_dir, tenant))``
        followed by the :class:`UserIdentityResolver` wrap.
        """
        path = identity_db_path(data_dir, tenant)
        store = await SqliteIdentityStore.open(path)
        return cls(store, channels=channels)

    async def close(self) -> None:
        await self._store.close()

    async def __aenter__(self) -> UserIdentityResolver:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Resolve / lookup
    # ------------------------------------------------------------------

    @property
    def store(self) -> SqliteIdentityStore:
        """Borrow the underlying store. Useful for tests and admin
        tooling that want the lower-level API."""
        return self._store

    @property
    def channels(self) -> ChannelRegistry:
        """The channel registry. Add adapters via ``channels.register(...)``."""
        return self._channels

    async def resolve(
        self,
        channel: str,
        channel_user_id: str,
        *,
        display_name_hint: str | None = None,
    ) -> UserId:
        """Resolve-or-create. See
        :meth:`SqliteIdentityStore.resolve_or_create`."""
        return await self._store.resolve_or_create(  # type: ignore[no-any-return]
            channel, channel_user_id, display_name_hint
        )

    async def lookup(
        self, channel: str, channel_user_id: str
    ) -> UserId | None:
        """Look up without minting."""
        return await self._store.lookup(channel, channel_user_id)  # type: ignore[no-any-return]

    async def aliases_for(self, user_id: UserId) -> list[ChannelAlias]:
        """Every alias bound to ``user_id``."""
        return await self._store.aliases_for(user_id)  # type: ignore[no-any-return]

    async def list_users(
        self, limit: int = 50, offset: int = 0
    ) -> list[UserSummary]:
        """Paginated user list."""
        return await self._store.list_users(limit, offset)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Linking (operator-driven merge)
    # ------------------------------------------------------------------

    async def link(
        self,
        into_user_id: UserId,
        from_user_id: UserId,
        *,
        decided_by: str,
    ) -> UserId:
        """Operator-driven manual merge. See
        :meth:`SqliteIdentityStore.merge_users`."""
        return await self._store.merge_users(  # type: ignore[no-any-return]
            into_user_id, from_user_id, decided_by
        )

    # ------------------------------------------------------------------
    # Verification workflow
    # ------------------------------------------------------------------

    async def verify_issue(
        self,
        user_id: UserId,
        channel: str,
        channel_user_id: str,
        *,
        echo: bool = True,
    ) -> VerificationPhrase:
        """Issue a phrase. If ``echo`` is true and a channel adapter is
        registered for ``channel``, push the phrase to the user.

        Operators that don't want the auto-echo (e.g. they'd rather
        copy-paste the phrase manually) pass ``echo=False``.
        """
        phrase = await self._store.issue_phrase(  # type: ignore[no-any-return]
            user_id, channel, channel_user_id
        )
        if echo:
            adapter = self._channels.get(channel)
            if adapter is not None:
                await adapter.echo_phrase(channel_user_id, phrase)
        return phrase

    async def verify_redeem(
        self,
        phrase: str,
        channel: str,
        channel_user_id: str,
    ) -> UserId:
        """Redeem a phrase the human just pasted on ``channel``."""
        return await self._store.redeem_phrase(  # type: ignore[no-any-return]
            phrase, channel, channel_user_id
        )

    async def sweep_expired_phrases(self) -> int:
        """GC expired, unconsumed phrases. See
        :meth:`SqliteIdentityStore.sweep_expired_phrases`."""
        return await self._store.sweep_expired_phrases()  # type: ignore[no-any-return]


__all__ = [
    "UserIdentityResolver",
]
