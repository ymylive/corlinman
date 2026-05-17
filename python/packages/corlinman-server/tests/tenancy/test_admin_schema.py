"""Port of ``corlinman-tenant::admin_schema`` async tests to pytest."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from corlinman_server.tenancy import (
    AdminDb,
    AdminExistsError,
    TenantExistsError,
    TenantId,
    hash_api_key_token,
)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[AdminDb]:
    """Fresh admin DB rooted in a per-test tempdir."""
    path = tmp_path / "tenants.sqlite"
    instance = await AdminDb.open(path)
    try:
        yield instance
    finally:
        await instance.close()


# ---------------------------------------------------------------------------
# Tenants + admins
# ---------------------------------------------------------------------------


async def test_open_creates_tables_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "tenants.sqlite"
    # Open twice; second open must not error and must observe an empty
    # roster.
    first = await AdminDb.open(path)
    await first.close()
    second = await AdminDb.open(path)
    try:
        assert await second.list_active() == []
    finally:
        await second.close()


async def test_create_tenant_round_trips(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme Corp", 1_700_000_000)

    row = await db.get(acme)
    assert row is not None, "just created"
    assert row.tenant_id == acme
    assert row.display_name == "Acme Corp"
    assert row.created_at == 1_700_000_000
    assert row.deleted_at is None

    listed = await db.list_active()
    assert len(listed) == 1
    assert listed[0].tenant_id == acme


async def test_create_tenant_rejects_duplicate_slug(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "First", 1)
    with pytest.raises(TenantExistsError):
        await db.create_tenant(acme, "Second", 2)


async def test_add_admin_round_trips_and_lists_in_username_order(
    db: AdminDb,
) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme Corp", 1)
    await db.add_admin(acme, "bob", "$argon2id$v=19$m=...$bobhash", 10)
    await db.add_admin(acme, "alice", "$argon2id$v=19$m=...$alicehash", 11)

    admins = await db.list_admins(acme)
    assert len(admins) == 2
    assert admins[0].username == "alice"
    assert admins[1].username == "bob"
    assert admins[0].password_hash.startswith("$argon2id$")


async def test_add_admin_rejects_duplicate_username_per_tenant(
    db: AdminDb,
) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme Corp", 1)
    await db.add_admin(acme, "alice", "$argon2id$h1", 1)
    with pytest.raises(AdminExistsError):
        await db.add_admin(acme, "alice", "$argon2id$h2", 2)


async def test_add_admin_fails_when_parent_tenant_missing(db: AdminDb) -> None:
    ghost = TenantId.new("ghost")
    # No `create_tenant` first → FK violation. The Python port surfaces
    # this as a raw `aiosqlite.IntegrityError` (or its sqlite3 parent),
    # mirroring the Rust path that returns a generic `Sqlx` error.
    with pytest.raises(aiosqlite.IntegrityError):
        await db.add_admin(ghost, "alice", "$argon2id$h", 1)


async def test_get_returns_none_for_unknown_tenant(db: AdminDb) -> None:
    nope = TenantId.new("never-existed")
    assert await db.get(nope) is None


# ---------------------------------------------------------------------------
# Federation peers (Phase 4 W2 B3 iter 1)
# ---------------------------------------------------------------------------


async def test_add_then_list_round_trip(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    bravo = TenantId.new("bravo")

    await db.add_federation_peer(acme, bravo, "alice")

    sources = await db.list_federation_sources_for(acme)
    assert len(sources) == 1
    assert sources[0].peer_tenant_id == acme
    assert sources[0].source_tenant_id == bravo
    assert sources[0].accepted_by == "alice"
    # Stamp must be a sane unix-millis (post-2001) without us having to
    # thread a clock.
    assert sources[0].accepted_at_ms > 1_000_000_000_000

    peers = await db.list_federation_peers_of(bravo)
    assert len(peers) == 1
    assert peers[0].peer_tenant_id == acme
    assert peers[0].source_tenant_id == bravo


async def test_add_is_idempotent_via_unique_pk(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    bravo = TenantId.new("bravo")

    await db.add_federation_peer(acme, bravo, "alice")
    # Second add must not error and must not duplicate the row.
    await db.add_federation_peer(acme, bravo, "bob")

    sources = await db.list_federation_sources_for(acme)
    assert len(sources) == 1, "INSERT OR IGNORE must not duplicate"
    # First-writer-wins on idempotent re-add: the original
    # `accepted_by` is preserved so callers can rely on the stored
    # value being the operator who actually accepted.
    assert sources[0].accepted_by == "alice"


async def test_remove_returns_true_on_hit_false_on_miss(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    bravo = TenantId.new("bravo")
    charlie = TenantId.new("charlie")

    await db.add_federation_peer(acme, bravo, "alice")

    # Hit: row exists, gets deleted.
    assert await db.remove_federation_peer(acme, bravo) is True

    # Miss: row already gone.
    assert await db.remove_federation_peer(acme, bravo) is False

    # Miss: pair never existed.
    assert await db.remove_federation_peer(acme, charlie) is False

    # Post-condition: no rows remain.
    assert await db.list_federation_sources_for(acme) == []


async def test_asymmetry_holds(db: AdminDb) -> None:
    # A → B opt-in (A accepts from B) must NOT show up when listing
    # what B accepts from. Asymmetric directional peering is the
    # entire point of the schema.
    a = TenantId.new("alpha")
    b = TenantId.new("bravo")

    await db.add_federation_peer(a, b, "alice")

    # A's perspective: yes, accepts from B.
    a_sources = await db.list_federation_sources_for(a)
    assert len(a_sources) == 1
    assert a_sources[0].source_tenant_id == b

    # B's perspective: accepts from nobody.
    b_sources = await db.list_federation_sources_for(b)
    assert b_sources == [], "B must not inherit A's opt-in"

    # From B's publishing side: A is a peer.
    b_peers = await db.list_federation_peers_of(b)
    assert len(b_peers) == 1
    assert b_peers[0].peer_tenant_id == a

    # From A's publishing side: nobody listens.
    a_peers = await db.list_federation_peers_of(a)
    assert a_peers == [], "A is not a source for anyone"


async def test_accepted_by_is_recorded(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    bravo = TenantId.new("bravo")
    charlie = TenantId.new("charlie")

    await db.add_federation_peer(acme, bravo, "alice-the-operator")
    await db.add_federation_peer(acme, charlie, "bob-the-operator")

    sources = await db.list_federation_sources_for(acme)
    # Ordered by source_tenant_id ASC: bravo before charlie.
    assert len(sources) == 2
    assert sources[0].source_tenant_id == bravo
    assert sources[0].accepted_by == "alice-the-operator"
    assert sources[1].source_tenant_id == charlie
    assert sources[1].accepted_by == "bob-the-operator"


async def test_list_active_excludes_soft_deleted(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    bravo = TenantId.new("bravo")
    await db.create_tenant(acme, "Acme", 1)
    await db.create_tenant(bravo, "Bravo", 2)

    # Soft-delete `bravo` directly via SQL — there's no public delete
    # API in v1 (Wave 2+), but the partial index + `deleted_at IS
    # NULL` filter must already DTRT.
    conn = db.connection()
    await conn.execute(
        "UPDATE tenants SET deleted_at = 99 WHERE tenant_id = ?",
        (bravo.as_str(),),
    )
    await conn.commit()

    active = await db.list_active()
    assert len(active) == 1
    assert active[0].tenant_id == acme

    # `get` still surfaces soft-deleted rows so operators can diagnose
    # why an expected tenant is missing from `list`.
    bravo_row = await db.get(bravo)
    assert bravo_row is not None
    assert bravo_row.deleted_at == 99


# ---------------------------------------------------------------------------
# API keys (Phase 4 W3 C4 iter 2)
# ---------------------------------------------------------------------------


async def test_mint_api_key_returns_cleartext_then_hashes_in_db(
    db: AdminDb,
) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme", 1)

    minted = await db.mint_api_key(acme, "alice", "chat", "MacBook")

    # Cleartext shape: `ck_` prefix + 64 hex chars.
    assert minted.token.startswith("ck_")
    assert len(minted.token) == 67

    # Stored row has hash, NOT cleartext.
    assert minted.row.token_hash != minted.token
    assert len(minted.row.token_hash) == 64  # sha256 hex
    assert minted.row.token_hash == hash_api_key_token(minted.token)
    assert minted.row.username == "alice"
    assert minted.row.scope == "chat"
    assert minted.row.label == "MacBook"
    assert minted.row.tenant_id == acme
    assert minted.row.last_used_at_ms is None
    assert minted.row.revoked_at_ms is None


async def test_mint_api_key_rejects_unknown_tenant_via_fk(db: AdminDb) -> None:
    ghost = TenantId.new("ghost")
    # No `create_tenant` first — FK violation surfaces as an
    # IntegrityError (matches Rust's generic Sqlx error path).
    with pytest.raises(aiosqlite.IntegrityError):
        await db.mint_api_key(ghost, "alice", "chat", None)


async def test_list_api_keys_orders_by_created_desc_and_excludes_revoked(
    db: AdminDb,
) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme", 1)

    k1 = await db.mint_api_key(acme, "alice", "chat", "first")
    # Sleep 2ms so created_at_ms advances; with millisecond precision
    # back-to-back inserts can land in the same tick.
    await asyncio.sleep(0.002)
    k2 = await db.mint_api_key(acme, "bob", "chat", "second")
    await asyncio.sleep(0.002)
    k3 = await db.mint_api_key(acme, "carol", "chat", "third")

    # Most recent first.
    listed = await db.list_api_keys(acme)
    assert len(listed) == 3
    assert listed[0].key_id == k3.row.key_id
    assert listed[1].key_id == k2.row.key_id
    assert listed[2].key_id == k1.row.key_id

    # Revoke the middle one — list excludes it.
    assert await db.revoke_api_key(k2.row.key_id) is True

    listed_after = await db.list_api_keys(acme)
    assert len(listed_after) == 2
    assert listed_after[0].key_id == k3.row.key_id
    assert listed_after[1].key_id == k1.row.key_id

    # Re-revoking is a no-op miss (idempotent).
    assert await db.revoke_api_key(k2.row.key_id) is False


async def test_verify_api_key_round_trip_and_bumps_last_used(
    db: AdminDb,
) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme", 1)
    minted = await db.mint_api_key(acme, "alice", "chat", None)

    # Sentinel value before verify so we can assert the bump moved it.
    assert minted.row.last_used_at_ms is None

    verified = await db.verify_api_key(minted.token)
    assert verified is not None, "freshly minted token must verify"
    assert verified.key_id == minted.row.key_id
    assert verified.tenant_id == acme
    assert verified.last_used_at_ms is not None

    # List view also sees the bump (re-read from DB, not from the
    # verify return value).
    listed = await db.list_api_keys(acme)
    assert len(listed) == 1
    assert listed[0].last_used_at_ms is not None


async def test_verify_api_key_rejects_unknown_token(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme", 1)
    _ = await db.mint_api_key(acme, "alice", "chat", None)

    assert await db.verify_api_key("ck_does_not_exist_12345") is None


async def test_verify_api_key_rejects_revoked_token(db: AdminDb) -> None:
    acme = TenantId.new("acme")
    await db.create_tenant(acme, "Acme", 1)
    minted = await db.mint_api_key(acme, "alice", "chat", None)

    # Sanity check: pre-revoke verify hits.
    assert await db.verify_api_key(minted.token) is not None

    # Revoke + post-revoke verify must miss even though the hash is
    # still present in the table.
    assert await db.revoke_api_key(minted.row.key_id) is True
    assert await db.verify_api_key(minted.token) is None


def test_hash_api_key_token_matches_known_sha256() -> None:
    # sha256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
    # — pinned so a stray hashing-impl swap surfaces here. This is the
    # exact pin the Rust crate uses.
    assert (
        hash_api_key_token("hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
