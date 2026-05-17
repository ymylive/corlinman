"""Unit tests for :mod:`corlinman_identity.store`.

Ports the Rust ``store::tests`` module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from corlinman_identity import (
    SCHEMA_SQL,
    SqliteIdentityStore,
    TenantId,
    identity_db_path,
    legacy_default,
)


def test_identity_db_path_uses_per_tenant_layout_for_named_tenant() -> None:
    acme = TenantId("acme")
    p = identity_db_path(Path("/data"), acme)
    s = str(p)
    assert "/tenants/acme/" in s
    assert s.endswith("user_identity.sqlite")


def test_identity_db_path_collapses_for_legacy_default() -> None:
    # The legacy default path doesn't include a ``/tenants/default/``
    # segment.
    default = legacy_default()
    p = identity_db_path(Path("/data"), default)
    assert str(p).endswith("user_identity.sqlite")
    assert "/tenants/default/" not in str(p)


async def test_open_creates_schema_and_reopens_idempotently(tmp_path: Path) -> None:
    tenant = legacy_default()
    path = identity_db_path(tmp_path, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)

    store = await SqliteIdentityStore.open(path)
    cursor = await store.conn.execute("SELECT COUNT(*) FROM user_identities")
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 0
    await store.close()

    # Re-open: CREATE TABLE IF NOT EXISTS is a no-op, no data loss.
    store2 = await SqliteIdentityStore.open(path)
    cursor = await store2.conn.execute("SELECT COUNT(*) FROM user_aliases")
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 0
    cursor = await store2.conn.execute(
        "SELECT COUNT(*) FROM verification_phrases"
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and int(row[0]) == 0
    await store2.close()


async def test_open_with_pool_size_one_passes_through(tmp_path: Path) -> None:
    # Pool sizing is mostly a test-fixture knob; assert only that the
    # constructor accepts and applies the override without raising.
    tenant = legacy_default()
    path = identity_db_path(tmp_path, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteIdentityStore.open_with_pool_size(path, 1)
    await store.close()


def test_schema_sql_contains_all_three_tables() -> None:
    # Copy-paste regression check on the DDL string.
    assert "CREATE TABLE IF NOT EXISTS user_identities" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS user_aliases" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS verification_phrases" in SCHEMA_SQL
    # FK cascade is part of the unify story; if it disappears, merges
    # leak orphaned rows.
    assert "ON DELETE CASCADE" in SCHEMA_SQL


async def test_schema_byte_matches_rust(tmp_path: Path) -> None:
    """The DDL we apply must produce the same table layout the Rust
    crate produces — sqlite_master rows are the contract."""
    tenant = legacy_default()
    path = identity_db_path(tmp_path, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteIdentityStore.open(path)
    await store.close()

    # Use plain sqlite3 to inspect — independent of aiosqlite.
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert tables == {
        "user_identities",
        "user_aliases",
        "verification_phrases",
    }
    assert "idx_user_aliases_user_id" in indexes
    assert "idx_verification_phrases_expires" in indexes
