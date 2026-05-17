"""Shared fixtures for the corlinman-evolution-store tests.

A fresh :class:`EvolutionStore` per test pinned to a temp directory so
test isolation does not depend on lock-step pool teardown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_evolution_store import EvolutionStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "evolution.sqlite"


@pytest_asyncio.fixture
async def store(db_path: Path) -> AsyncIterator[EvolutionStore]:
    """Yield an opened :class:`EvolutionStore` and clean it up after the
    test. Mirrors the Rust ``fresh_store()`` helper."""
    s = await EvolutionStore.open(db_path)
    try:
        yield s
    finally:
        await s.close()
