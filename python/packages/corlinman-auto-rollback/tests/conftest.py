"""Shared fixtures for the corlinman-auto-rollback tests.

Mirrors the ``fresh_store()`` helper in
``rust/crates/corlinman-auto-rollback/src/monitor.rs::tests``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_evolution_store import EvolutionStore, HistoryRepo, ProposalsRepo


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "evolution.sqlite"


@pytest_asyncio.fixture
async def store(db_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(db_path)
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def repos(
    store: EvolutionStore,
) -> AsyncIterator[tuple[ProposalsRepo, HistoryRepo]]:
    """Wrap the shared connection in fresh repo handles. Mirrors the
    Rust ``(_tmp, store, proposals, history)`` quadruple — the store
    is yielded by its own fixture."""
    yield ProposalsRepo(store.conn), HistoryRepo(store.conn)
