"""Port of ``corlinman-tenant::pool`` async tests to pytest-asyncio."""

from __future__ import annotations

import asyncio
from pathlib import Path

from corlinman_server.tenancy import TenantId, TenantPool


def _acme() -> TenantId:
    return TenantId.new("acme")


def _bravo() -> TenantId:
    return TenantId.new("bravo")


async def test_opens_connection_lazily_and_caches(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)
    tenant = _acme()

    assert not await pool.is_cached(tenant, "evolution")

    c1 = await pool.get_or_open(tenant, "evolution")
    assert await pool.is_cached(tenant, "evolution")

    c2 = await pool.get_or_open(tenant, "evolution")
    # aiosqlite returns the *same* cached connection — identity check
    # is the strongest assertion that "share" semantics are preserved.
    assert c1 is c2

    # Sanity-check that the cached connection actually works.
    async with c1.execute("SELECT 1") as cursor:
        row = await cursor.fetchone()
    assert row == (1,)

    await pool.close_all()


async def test_isolates_pools_per_tenant(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)

    # Each tenant gets its own DB file.
    _a = await pool.get_or_open(_acme(), "evolution")
    _b = await pool.get_or_open(_bravo(), "evolution")

    p_acme = tmp_path / "tenants" / "acme" / "evolution.sqlite"
    p_bravo = tmp_path / "tenants" / "bravo" / "evolution.sqlite"
    assert p_acme.exists(), f"acme db should exist at {p_acme}"
    assert p_bravo.exists(), f"bravo db should exist at {p_bravo}"

    # Distinct files: writing to one does not appear in the other.
    acme_conn = await pool.get_or_open(_acme(), "evolution")
    await acme_conn.execute("CREATE TABLE marker (x INTEGER)")
    await acme_conn.commit()

    bravo_conn = await pool.get_or_open(_bravo(), "evolution")
    async with bravo_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='marker'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is None, "bravo db must not see acme marker table"

    await pool.close_all()


async def test_isolates_pools_per_db_name(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)
    tenant = _acme()

    _evol = await pool.get_or_open(tenant, "evolution")
    _kb = await pool.get_or_open(tenant, "kb")

    assert (tmp_path / "tenants" / "acme" / "evolution.sqlite").exists()
    assert (tmp_path / "tenants" / "acme" / "kb.sqlite").exists()

    await pool.close_all()


async def test_db_path_does_not_create_file(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)
    tenant = _acme()

    # Probing the path is allowed before open; it must not touch the
    # filesystem (admin / migration code relies on this).
    p = pool.db_path(tenant, "evolution")
    assert p == tmp_path / "tenants" / "acme" / "evolution.sqlite"
    assert not p.exists()


async def test_concurrent_first_open_does_not_panic_or_deadlock(
    tmp_path: Path,
) -> None:
    # Two tasks racing on the same (tenant, db) pair. The single
    # asyncio.Lock in `get_or_open` should serialise opens and one of
    # them observes the cached connection on the slow path.
    pool = TenantPool(tmp_path)
    t = _acme()

    async def opener() -> bool:
        conn = await pool.get_or_open(t, "evolution")
        return conn is not None

    h1 = asyncio.create_task(opener())
    h2 = asyncio.create_task(opener())
    assert await h1
    assert await h2

    # Exactly one cached entry.
    assert await pool.is_cached(t, "evolution")

    await pool.close_all()


async def test_root_accessor_returns_path(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)
    assert pool.root() == tmp_path


async def test_with_max_connections_returns_self_for_chaining(
    tmp_path: Path,
) -> None:
    pool = TenantPool(tmp_path).with_max_connections(16)
    # Builder is chainable like the Rust version; the override is
    # stored even though the aiosqlite port doesn't use it yet.
    assert pool._max_connections == 16


async def test_close_all_is_idempotent(tmp_path: Path) -> None:
    pool = TenantPool(tmp_path)
    _ = await pool.get_or_open(_acme(), "evolution")
    await pool.close_all()
    # Second call must not raise.
    await pool.close_all()
    # And the cache is empty so a fresh open works.
    _ = await pool.get_or_open(_acme(), "evolution")
    await pool.close_all()


async def test_pragmas_are_applied(tmp_path: Path) -> None:
    """Both ``journal_mode=WAL`` and ``foreign_keys=ON`` must take
    effect on every connection — they're the codebase invariants."""
    pool = TenantPool(tmp_path)
    conn = await pool.get_or_open(_acme(), "evolution")
    async with conn.execute("PRAGMA journal_mode") as cursor:
        mode = await cursor.fetchone()
    assert mode is not None and str(mode[0]).lower() == "wal"

    async with conn.execute("PRAGMA foreign_keys") as cursor:
        fk = await cursor.fetchone()
    assert fk is not None and int(fk[0]) == 1

    await pool.close_all()
