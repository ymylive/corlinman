"""Shared fixtures for the corlinman-shadow-tester tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from corlinman_evolution_store import EvolutionStore, ProposalsRepo


FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "eval"


@pytest.fixture
def fixtures_root() -> Path:
    return FIXTURES_ROOT


@pytest.fixture
def evolution_db_path(tmp_path: Path) -> Path:
    return tmp_path / "evolution.sqlite"


@pytest_asyncio.fixture
async def store(evolution_db_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(evolution_db_path)
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def proposals_repo(store: EvolutionStore) -> ProposalsRepo:
    return ProposalsRepo(store.conn)
