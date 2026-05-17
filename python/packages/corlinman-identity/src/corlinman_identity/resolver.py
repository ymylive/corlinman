"""``IdentityStore`` protocol + the resolver methods on :class:`SqliteIdentityStore`.

Direct port of ``rust/crates/corlinman-identity/src/resolver.rs``. The
single async surface the gateway middleware and admin routes call into.

Method signatures match the Rust trait 1:1:

* :meth:`SqliteIdentityStore.resolve_or_create`
* :meth:`SqliteIdentityStore.lookup`
* :meth:`SqliteIdentityStore.aliases_for`
* :meth:`SqliteIdentityStore.list_users`
* :meth:`SqliteIdentityStore.issue_phrase` (defined in
  :mod:`corlinman_identity.verification`)
* :meth:`SqliteIdentityStore.merge_users`

Tenant scoping happens at the *store* layer — the caller selects which
tenant they're operating against by picking which
``<data_dir>/tenants/<slug>/user_identity.sqlite`` file the store was
opened from. Per-call tenant arguments would be redundant and risk
drift between the path and the column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import aiosqlite

from corlinman_identity.error import (
    InvalidInputError,
    StorageError,
    UserNotFoundError,
)
from corlinman_identity.store import SqliteIdentityStore
from corlinman_identity.types import (
    BindingKind,
    ChannelAlias,
    UserId,
    UserSummary,
    VerificationPhrase,
)


# ---------------------------------------------------------------------------
# Storage-agnostic surface
# ---------------------------------------------------------------------------


@runtime_checkable
class IdentityStore(Protocol):
    """Storage-agnostic surface for identity resolution.

    Mirrors the Rust ``IdentityStore`` trait. :class:`SqliteIdentityStore`
    is the only built-in impl, but downstream tests / admin tooling can
    pass any class that matches this protocol.
    """

    async def resolve_or_create(
        self,
        channel: str,
        channel_user_id: str,
        display_name_hint: str | None = None,
    ) -> UserId:
        """Resolve the canonical :class:`UserId` for an incoming message.

        If the ``(channel, channel_user_id)`` pair is already known,
        returns the bound ``user_id``. If new, mints a fresh ``user_id``
        and records a :attr:`BindingKind.AUTO` alias for it.

        Idempotent under concurrent first-call races: two simultaneous
        callers for the same pair both observe the same ``UserId``.
        """
        ...

    async def lookup(
        self,
        channel: str,
        channel_user_id: str,
    ) -> UserId | None:
        """Look up without minting.

        Returns ``None`` when the alias is unknown — used by admin
        surfaces and tooling that want a "does this alias exist yet?"
        check without side effects.
        """
        ...

    async def aliases_for(self, user_id: UserId) -> list[ChannelAlias]:
        """Every alias bound to ``user_id``.

        Used by ``/admin/identity/:user_id`` and by the trait-merge job
        to enumerate channels for a unified user. Empty list when the
        user has no aliases.
        """
        ...

    async def list_users(self, limit: int, offset: int) -> list[UserSummary]:
        """Page through ``user_identities``, ordered by ``created_at DESC``.

        ``limit`` is clamped to ``[1, 200]`` at the call site to keep an
        unbounded ``LIMIT 0`` query from being expensive on a tenant
        with millions of users.
        """
        ...

    async def issue_phrase(
        self,
        user_id: UserId,
        channel: str,
        channel_user_id: str,
    ) -> VerificationPhrase:
        """Issue a fresh verification phrase for ``user_id`` on
        ``(channel, channel_user_id)``."""
        ...

    async def merge_users(
        self,
        into_user_id: UserId,
        from_user_id: UserId,
        decided_by: str,
    ) -> UserId:
        """Operator-driven manual merge.

        Reattributes every alias bound to ``from_user_id`` to
        ``into_user_id`` with ``binding_kind = 'operator'``, then
        deletes the orphaned ``from_user_id`` row.
        """
        ...


# ---------------------------------------------------------------------------
# RFC-3339 helpers
# ---------------------------------------------------------------------------

# The Rust crate uses ``time::format_description::well_known::Rfc3339``,
# which always emits a ``Z`` suffix or an explicit offset. Python's
# ``datetime.isoformat()`` produces "+00:00" — fine on the wire, but we
# normalise to ``Z`` so the strings byte-match what the Rust side writes
# and SQLite-level comparisons line up across implementations.


def _now_utc_rfc3339() -> str:
    """Current UTC time in RFC-3339 with ``Z`` suffix."""
    return _to_rfc3339(datetime.now(timezone.utc))


def _to_rfc3339(dt: datetime) -> str:
    """Format ``dt`` as RFC-3339 with a ``Z`` suffix for UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.isoformat()
    # ``isoformat`` always uses ``+00:00`` for UTC; normalise to ``Z``
    # so we exactly match the Rust serialiser.
    if iso.endswith("+00:00"):
        return iso[:-6] + "Z"
    return iso


def _parse_rfc3339(raw: str) -> datetime:
    """Parse an RFC-3339 timestamp written by either Rust or Python.

    Accepts ``Z`` suffix or ``+HH:MM`` offset. Returns a tz-aware
    :class:`datetime`.
    """
    # ``fromisoformat`` on 3.11+ handles ``Z`` natively, but we keep the
    # branch explicit so a 3.12 build doesn't subtly depend on the
    # forward-compat handling.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


# ---------------------------------------------------------------------------
# Helpers for the SqliteIdentityStore impl
# ---------------------------------------------------------------------------


def _is_unique_violation(exc: BaseException) -> bool:
    """Identify a UNIQUE-constraint violation in a sqlite3 error.

    SQLite surfaces these as :class:`sqlite3.IntegrityError` whose
    ``args[0]`` contains ``"UNIQUE constraint failed"``. String-matching
    is fragile across sqlite versions in principle but pinned in
    practice — the wire format hasn't changed for years.
    """
    msg = " ".join(str(a) for a in getattr(exc, "args", ()))
    return "UNIQUE constraint failed" in msg


# ---------------------------------------------------------------------------
# Resolver methods bolted onto SqliteIdentityStore
# ---------------------------------------------------------------------------


async def _lookup(
    conn: aiosqlite.Connection, channel: str, channel_user_id: str
) -> UserId | None:
    """Crate-private helper shared by the resolver paths."""
    try:
        cursor = await conn.execute(
            "SELECT user_id FROM user_aliases "
            "WHERE channel = ?1 AND channel_user_id = ?2",
            (channel, channel_user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
    except Exception as exc:
        raise StorageError(op="lookup", source=exc) from exc
    if row is None:
        return None
    return UserId(str(row[0]))


async def _insert_new_user_and_alias(
    conn: aiosqlite.Connection,
    channel: str,
    channel_user_id: str,
    display_name_hint: str | None,
) -> UserId:
    """Write a fresh ``(user_identities, user_aliases)`` pair atomically.

    Raises :class:`StorageError` whose ``source`` is the underlying
    :class:`sqlite3.Error` so the caller can introspect for a UNIQUE
    conflict and retry the read path.
    """
    user_id = UserId.generate()
    now = _now_utc_rfc3339()

    try:
        await conn.execute("BEGIN")
    except Exception as exc:
        raise StorageError(op="begin", source=exc) from exc

    try:
        try:
            await conn.execute(
                "INSERT INTO user_identities "
                "(user_id, display_name, created_at, updated_at, confidence) "
                "VALUES (?1, ?2, ?3, ?3, 1.0)",
                (str(user_id), display_name_hint, now),
            )
        except Exception as exc:
            raise StorageError(op="insert_user_identity", source=exc) from exc

        try:
            await conn.execute(
                "INSERT INTO user_aliases "
                "(channel, channel_user_id, user_id, created_at, binding_kind) "
                "VALUES (?1, ?2, ?3, ?4, 'auto')",
                (channel, channel_user_id, str(user_id), now),
            )
        except Exception as exc:
            raise StorageError(op="insert_user_alias", source=exc) from exc

        try:
            await conn.commit()
        except Exception as exc:
            raise StorageError(op="commit", source=exc) from exc
    except BaseException:
        # Roll back on any failure so a UNIQUE violation on the second
        # INSERT doesn't leave the orphaned user_identities row behind.
        await conn.rollback()
        raise

    return user_id


async def _resolve_or_create(
    store: SqliteIdentityStore,
    channel: str,
    channel_user_id: str,
    display_name_hint: str | None = None,
) -> UserId:
    if not channel:
        raise InvalidInputError("channel must be non-empty")
    if not channel_user_id:
        raise InvalidInputError("channel_user_id must be non-empty")

    # Fast path: the alias already exists.
    existing = await _lookup(store.conn, channel, channel_user_id)
    if existing is not None:
        return existing

    # Slow path. The ``BEGIN..COMMIT`` block has to be serialised across
    # interleaved coroutines because we share a single underlying
    # connection — without the lock, a second coroutine's ``BEGIN``
    # would land while the first is still inside its transaction.
    # On UNIQUE conflict, re-read so a racing first-caller's row
    # becomes the loser's return value.
    async with store.tx_lock:
        # Re-check inside the lock: while we were waiting, another
        # coroutine may have already minted the row we wanted.
        existing = await _lookup(store.conn, channel, channel_user_id)
        if existing is not None:
            return existing
        try:
            return await _insert_new_user_and_alias(
                store.conn, channel, channel_user_id, display_name_hint
            )
        except StorageError as err:
            if _is_unique_violation(err.source):
                retried = await _lookup(store.conn, channel, channel_user_id)
                if retried is None:
                    raise StorageError(
                        op="resolve_or_create_retry",
                        source=RuntimeError(
                            "UNIQUE conflict but row not visible"
                        ),
                    ) from err
                return retried
            raise


async def _aliases_for(
    store: SqliteIdentityStore, user_id: UserId
) -> list[ChannelAlias]:
    try:
        cursor = await store.conn.execute(
            "SELECT channel, channel_user_id, user_id, created_at, binding_kind "
            "FROM user_aliases "
            "WHERE user_id = ?1 "
            "ORDER BY created_at ASC",
            (str(user_id),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    except Exception as exc:
        raise StorageError(op="aliases_for", source=exc) from exc

    out: list[ChannelAlias] = []
    for row in rows:
        channel = str(row[0])
        channel_user_id = str(row[1])
        user_id_str = str(row[2])
        created_at_str = str(row[3])
        binding_kind_str = str(row[4])
        try:
            created_at = _parse_rfc3339(created_at_str)
        except ValueError as exc:
            raise StorageError(op="aliases_for_parse_ts", source=exc) from exc
        out.append(
            ChannelAlias(
                channel=channel,
                channel_user_id=channel_user_id,
                user_id=UserId(user_id_str),
                binding_kind=BindingKind.from_db_str(binding_kind_str),
                created_at=created_at,
            )
        )
    return out


async def _list_users(
    store: SqliteIdentityStore, limit: int, offset: int
) -> list[UserSummary]:
    # Clamp to [1, 200] — matches the Rust ``limit.clamp(1, 200)`` so an
    # unbounded ``LIMIT 0`` doesn't full-scan a populated tenant.
    clamped = max(1, min(200, int(limit)))
    off = max(0, int(offset))
    try:
        cursor = await store.conn.execute(
            "SELECT u.user_id, u.display_name, COUNT(a.channel) AS alias_count "
            "FROM user_identities u "
            "LEFT JOIN user_aliases a ON a.user_id = u.user_id "
            "GROUP BY u.user_id "
            "ORDER BY u.created_at DESC "
            "LIMIT ?1 OFFSET ?2",
            (clamped, off),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    except Exception as exc:
        raise StorageError(op="list_users", source=exc) from exc

    return [
        UserSummary(
            user_id=UserId(str(row[0])),
            display_name=(None if row[1] is None else str(row[1])),
            alias_count=int(row[2]),
        )
        for row in rows
    ]


async def _merge_users(
    store: SqliteIdentityStore,
    into_user_id: UserId,
    from_user_id: UserId,
    decided_by: str,
) -> UserId:
    if str(into_user_id) == str(from_user_id):
        raise InvalidInputError("into_user_id and from_user_id must differ")
    if not decided_by:
        raise InvalidInputError("decided_by must be non-empty")

    # Same single-connection serialisation story as
    # :func:`_resolve_or_create` — the ``BEGIN..COMMIT`` block must be
    # held against any interleaved coroutine that might also try to
    # start a transaction.
    async with store.tx_lock:
        return await _merge_users_locked(
            store, into_user_id, from_user_id, decided_by
        )


async def _merge_users_locked(
    store: SqliteIdentityStore,
    into_user_id: UserId,
    from_user_id: UserId,
    decided_by: str,
) -> UserId:
    try:
        await store.conn.execute("BEGIN")
    except Exception as exc:
        raise StorageError(op="merge_users_begin", source=exc) from exc

    try:
        # Both rows must exist before any reattribution.
        try:
            cursor = await store.conn.execute(
                "SELECT COUNT(*) FROM user_identities WHERE user_id = ?1",
                (str(into_user_id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            raise StorageError(op="merge_users_check_into", source=exc) from exc
        if not row or int(row[0]) == 0:
            raise UserNotFoundError(str(into_user_id))

        try:
            cursor = await store.conn.execute(
                "SELECT COUNT(*) FROM user_identities WHERE user_id = ?1",
                (str(from_user_id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            raise StorageError(op="merge_users_check_from", source=exc) from exc
        if not row or int(row[0]) == 0:
            raise UserNotFoundError(str(from_user_id))

        # Reattribute every alias on the source to the target.
        try:
            await store.conn.execute(
                "UPDATE user_aliases "
                "SET user_id = ?1, binding_kind = 'operator' "
                "WHERE user_id = ?2",
                (str(into_user_id), str(from_user_id)),
            )
        except Exception as exc:
            raise StorageError(
                op="merge_users_reattribute_aliases", source=exc
            ) from exc

        # Drop the orphaned source row.
        try:
            await store.conn.execute(
                "DELETE FROM user_identities WHERE user_id = ?1",
                (str(from_user_id),),
            )
        except Exception as exc:
            raise StorageError(op="merge_users_delete_orphan", source=exc) from exc

        # Touch the surviving row's updated_at.
        now = _now_utc_rfc3339()
        try:
            await store.conn.execute(
                "UPDATE user_identities SET updated_at = ?1 WHERE user_id = ?2",
                (now, str(into_user_id)),
            )
        except Exception as exc:
            raise StorageError(op="merge_users_touch_into", source=exc) from exc

        try:
            await store.conn.commit()
        except Exception as exc:
            raise StorageError(op="merge_users_commit", source=exc) from exc
    except BaseException:
        await store.conn.rollback()
        raise

    # ``decided_by`` isn't yet persisted — matches the Rust crate's
    # tracing-only audit trail. The audit-log surface (Phase 4 W2
    # follow-up) will pick it up.
    return into_user_id


# ---------------------------------------------------------------------------
# Bind the methods onto SqliteIdentityStore at import time
# ---------------------------------------------------------------------------


async def _store_resolve_or_create(
    self: SqliteIdentityStore,
    channel: str,
    channel_user_id: str,
    display_name_hint: str | None = None,
) -> UserId:
    return await _resolve_or_create(self, channel, channel_user_id, display_name_hint)


async def _store_lookup(
    self: SqliteIdentityStore, channel: str, channel_user_id: str
) -> UserId | None:
    return await _lookup(self.conn, channel, channel_user_id)


async def _store_aliases_for(
    self: SqliteIdentityStore, user_id: UserId
) -> list[ChannelAlias]:
    return await _aliases_for(self, user_id)


async def _store_list_users(
    self: SqliteIdentityStore, limit: int, offset: int
) -> list[UserSummary]:
    return await _list_users(self, limit, offset)


async def _store_merge_users(
    self: SqliteIdentityStore,
    into_user_id: UserId,
    from_user_id: UserId,
    decided_by: str,
) -> UserId:
    return await _merge_users(self, into_user_id, from_user_id, decided_by)


# Monkey-patching the methods onto :class:`SqliteIdentityStore` keeps the
# store module focused on lifecycle while the resolver module owns the
# query surface — mirrors the Rust split between ``store.rs`` (the
# handle) and ``resolver.rs`` (the trait impl).
SqliteIdentityStore.resolve_or_create = _store_resolve_or_create  # type: ignore[attr-defined]
SqliteIdentityStore.lookup = _store_lookup  # type: ignore[attr-defined]
SqliteIdentityStore.aliases_for = _store_aliases_for  # type: ignore[attr-defined]
SqliteIdentityStore.list_users = _store_list_users  # type: ignore[attr-defined]
SqliteIdentityStore.merge_users = _store_merge_users  # type: ignore[attr-defined]


__all__ = [
    "IdentityStore",
]
