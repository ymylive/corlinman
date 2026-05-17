"""Shared fixtures for ``corlinman_identity`` tests.

Ports the Rust tests' ``fresh(&TempDir)`` helper to a pytest fixture
returning a ready-to-use :class:`SqliteIdentityStore` open on a fresh
per-test directory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from corlinman_identity import (
    SqliteIdentityStore,
    identity_db_path,
    legacy_default,
)


@pytest_asyncio.fixture
async def fresh_store(tmp_path: Path) -> AsyncIterator[SqliteIdentityStore]:
    """A fresh, schema-bootstrapped store under the legacy-default tenant.

    Mirrors the Rust ``fresh(&TempDir)`` helper used across the source
    crate's test modules.
    """
    tenant = legacy_default()
    path = identity_db_path(tmp_path, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteIdentityStore.open(path)
    try:
        yield store
    finally:
        await store.close()
