"""Shared fixtures for ``corlinman-episodes`` tests.

The package writes its own ``episodes.sqlite`` but reads from peer
databases owned by the Rust gateway and the evolution engine. Rather
than depend on those crates at test time we replicate the minimal
table shapes here — the exact column subset the
:mod:`corlinman_episodes.sources` collectors query.

Schemas are deliberately a subset of the production tables; any
production-side migration that adds a column the collectors care
about must also land here, and the collector's test will fail loudly
if the join goes wrong.

Insert helpers live in ``tests/_seed.py`` so test modules can import
them by name via ``from ._seed import ...`` (pytest's ``conftest.py``
only re-exports fixtures under ``importlib`` import-mode, not raw
functions).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

# Mirror of the Phase-4-tenant-aware ``sessions`` table from
# ``rust/crates/corlinman-core/src/session_sqlite.rs``. ``ts`` stays
# TEXT here so the collector exercises its RFC3339 parsing path —
# the prod DB also stores RFC3339, so the test path is realistic.
SESSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_calls_json TEXT,
    ts TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (session_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_key
    ON sessions(tenant_id, session_key, seq);
"""

# Mirror of ``rust/crates/corlinman-evolution/src/schema.rs`` —
# evolution_signals + evolution_proposals + evolution_history
# (both Phase 4 W1 4-1A tenant-aware variants). Column shapes line
# up with the evolution-engine conftest so cross-crate test fixtures
# stay consistent.
EVOLUTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolution_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    target TEXT,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id TEXT,
    session_id TEXT,
    observed_at INTEGER NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS evolution_proposals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    diff TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    risk TEXT NOT NULL,
    budget_cost INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    shadow_metrics TEXT,
    signal_ids TEXT NOT NULL,
    trace_ids TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by TEXT,
    applied_at INTEGER,
    rollback_of TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS evolution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    before_sha TEXT NOT NULL,
    after_sha TEXT NOT NULL,
    inverse_diff TEXT NOT NULL,
    metrics_baseline TEXT NOT NULL,
    applied_at INTEGER NOT NULL,
    rolled_back_at INTEGER,
    rollback_reason TEXT
);
"""

# Lean mirror of the Phase 4 W1.5 hook-events table — only the
# columns the source collector queries. The prod table has more
# fields; we don't care about them for the join.
HOOK_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT,
    session_key TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hook_events_tenant_kind
    ON hook_events(tenant_id, kind, occurred_at DESC);
"""

# Phase 4 W2 B2 verification_phrases — minimal subset.
IDENTITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS verification_phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a TEXT NOT NULL,
    user_b TEXT NOT NULL,
    channel TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    consumed_at INTEGER
);
"""


def _init_db(path: Path, schema: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def sessions_db(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "sessions.sqlite"
    _init_db(p, SESSIONS_SCHEMA_SQL)
    yield p


@pytest.fixture
def evolution_db(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "evolution.sqlite"
    _init_db(p, EVOLUTION_SCHEMA_SQL)
    yield p


@pytest.fixture
def hook_events_db(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "hook_events.sqlite"
    _init_db(p, HOOK_EVENTS_SCHEMA_SQL)
    yield p


@pytest.fixture
def identity_db(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "identity.sqlite"
    _init_db(p, IDENTITY_SCHEMA_SQL)
    yield p
