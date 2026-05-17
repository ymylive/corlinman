"""Unit tests for :mod:`corlinman_identity.resolver`.

Ports the Rust ``resolver::tests`` module.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_identity import (
    BindingKind,
    InvalidInputError,
    SqliteIdentityStore,
    UserId,
    UserNotFoundError,
)


async def test_resolve_or_create_mints_for_unknown_pair(
    fresh_store: SqliteIdentityStore,
) -> None:
    user_id = await fresh_store.resolve_or_create("qq", "1234", "Alice")
    assert len(user_id.as_str()) == 26  # ULID


async def test_resolve_or_create_returns_same_id_on_repeat(
    fresh_store: SqliteIdentityStore,
) -> None:
    first = await fresh_store.resolve_or_create("qq", "1234", None)
    second = await fresh_store.resolve_or_create("qq", "1234", None)
    assert first == second


async def test_resolve_or_create_distinct_for_different_channels(
    fresh_store: SqliteIdentityStore,
) -> None:
    qq = await fresh_store.resolve_or_create("qq", "1234", None)
    tg = await fresh_store.resolve_or_create("telegram", "1234", None)
    assert qq != tg


async def test_resolve_or_create_serialised_first_calls_yield_one_id(
    fresh_store: SqliteIdentityStore,
) -> None:
    """The Rust crate spawns 32 concurrent tasks and asserts all see
    the same id. aiosqlite serialises through a single connection by
    design, so 'concurrent' for us means interleaved awaits — the
    fast-path lookup still has to converge on one minted user."""
    coros = [
        fresh_store.resolve_or_create("qq", "1234", None) for _ in range(32)
    ]
    ids = await asyncio.gather(*coros)
    first = ids[0]
    for other in ids[1:]:
        assert other == first


async def test_lookup_returns_none_for_unknown(
    fresh_store: SqliteIdentityStore,
) -> None:
    assert await fresh_store.lookup("qq", "missing") is None


async def test_lookup_returns_user_id_after_resolve(
    fresh_store: SqliteIdentityStore,
) -> None:
    minted = await fresh_store.resolve_or_create("qq", "777", None)
    looked_up = await fresh_store.lookup("qq", "777")
    assert minted == looked_up


async def test_aliases_for_returns_all_bindings_in_creation_order(
    fresh_store: SqliteIdentityStore,
) -> None:
    uid = await fresh_store.resolve_or_create("qq", "primary", "Alice")
    # Manually bind a second alias via direct SQL — mirrors the Rust test.
    # Use a clearly-future timestamp so the ORDER BY created_at ASC puts
    # this row second (the Rust test relies on OffsetDateTime::now_utc()'s
    # microsecond resolution to keep the order; we pick a static future
    # date instead to avoid same-second ties).
    await fresh_store.conn.execute(
        "INSERT INTO user_aliases "
        "(channel, channel_user_id, user_id, created_at, binding_kind) "
        "VALUES (?, ?, ?, ?, 'verified')",
        ("telegram", "9876", str(uid), "2099-01-01T00:00:00Z"),
    )
    await fresh_store.conn.commit()

    aliases = await fresh_store.aliases_for(uid)
    assert len(aliases) == 2
    # ORDER BY created_at ASC; QQ was minted first.
    assert aliases[0].channel == "qq"
    assert aliases[0].binding_kind is BindingKind.AUTO
    assert aliases[1].channel == "telegram"
    assert aliases[1].binding_kind is BindingKind.VERIFIED
    assert aliases[0].user_id == uid
    assert aliases[1].user_id == uid


async def test_aliases_for_unknown_user_returns_empty(
    fresh_store: SqliteIdentityStore,
) -> None:
    phantom = UserId.generate()
    assert await fresh_store.aliases_for(phantom) == []


async def test_list_users_returns_descending_by_created_at_with_alias_counts(
    fresh_store: SqliteIdentityStore,
) -> None:
    u1 = await fresh_store.resolve_or_create("qq", "1", None)
    u2 = await fresh_store.resolve_or_create("qq", "2", None)
    u3 = await fresh_store.resolve_or_create("qq", "3", "Charlie")

    # Bond a second alias to u1 → alias_count = 2.
    await fresh_store.conn.execute(
        "INSERT INTO user_aliases "
        "(channel, channel_user_id, user_id, created_at, binding_kind) "
        "VALUES ('telegram', '999', ?, ?, 'verified')",
        (str(u1), "2026-04-28T09:00:00Z"),
    )
    await fresh_store.conn.commit()

    users = await fresh_store.list_users(10, 0)
    assert len(users) == 3
    # ORDER BY created_at DESC → u3 first.
    assert users[0].user_id == u3
    assert users[0].display_name == "Charlie"
    assert users[0].alias_count == 1
    assert users[1].user_id == u2
    assert users[1].alias_count == 1
    assert users[2].user_id == u1
    assert users[2].alias_count == 2


async def test_list_users_paginates_via_limit_offset(
    fresh_store: SqliteIdentityStore,
) -> None:
    for i in range(5):
        await fresh_store.resolve_or_create("qq", str(i), None)
    page1 = await fresh_store.list_users(2, 0)
    page2 = await fresh_store.list_users(2, 2)
    page3 = await fresh_store.list_users(2, 4)
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    p1_ids = {u.user_id for u in page1}
    for u in page2:
        assert u.user_id not in p1_ids


async def test_list_users_clamps_excessive_limit(
    fresh_store: SqliteIdentityStore,
) -> None:
    for i in range(5):
        await fresh_store.resolve_or_create("qq", str(i), None)
    with_zero = await fresh_store.list_users(0, 0)
    assert len(with_zero) == 1  # clamped to 1
    with_max = await fresh_store.list_users(2**31 - 1, 0)
    assert len(with_max) == 5  # 5 < clamp ceiling of 200


async def test_resolve_or_create_rejects_empty_channel(
    fresh_store: SqliteIdentityStore,
) -> None:
    with pytest.raises(InvalidInputError):
        await fresh_store.resolve_or_create("", "1234", None)


async def test_resolve_or_create_rejects_empty_channel_user_id(
    fresh_store: SqliteIdentityStore,
) -> None:
    with pytest.raises(InvalidInputError):
        await fresh_store.resolve_or_create("qq", "", None)


async def test_merge_users_reattributes_aliases_and_deletes_source(
    fresh_store: SqliteIdentityStore,
) -> None:
    into = await fresh_store.resolve_or_create("qq", "1234", None)
    from_ = await fresh_store.resolve_or_create("telegram", "9876", None)
    assert into != from_

    surviving = await fresh_store.merge_users(into, from_, "operator-alice")
    assert surviving == into

    # Telegram alias now points to ``into``.
    tg_now = await fresh_store.lookup("telegram", "9876")
    assert tg_now == into

    # The orphan is gone.
    cursor = await fresh_store.conn.execute(
        "SELECT COUNT(*) FROM user_identities WHERE user_id = ?",
        (str(from_),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 0

    # The reattributed alias is marked ``operator``.
    cursor = await fresh_store.conn.execute(
        "SELECT binding_kind FROM user_aliases "
        "WHERE channel = 'telegram' AND channel_user_id = '9876'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and str(row[0]) == "operator"


async def test_merge_users_rejects_self_merge(
    fresh_store: SqliteIdentityStore,
) -> None:
    uid = await fresh_store.resolve_or_create("qq", "1234", None)
    with pytest.raises(InvalidInputError):
        await fresh_store.merge_users(uid, uid, "operator-alice")


async def test_merge_users_404s_when_into_missing(
    fresh_store: SqliteIdentityStore,
) -> None:
    from_ = await fresh_store.resolve_or_create("qq", "1234", None)
    phantom = UserId.generate()
    with pytest.raises(UserNotFoundError):
        await fresh_store.merge_users(phantom, from_, "operator-alice")


async def test_merge_users_404s_when_from_missing(
    fresh_store: SqliteIdentityStore,
) -> None:
    into = await fresh_store.resolve_or_create("qq", "1234", None)
    phantom = UserId.generate()
    with pytest.raises(UserNotFoundError):
        await fresh_store.merge_users(into, phantom, "operator-alice")
