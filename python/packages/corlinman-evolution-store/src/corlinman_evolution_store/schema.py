"""Authoritative SQLite schema for the EvolutionLoop.

Ported 1:1 from ``rust/crates/corlinman-evolution/src/schema.rs``. Cross-
language contract — Python engine and Rust observer / admin API both bind
to these tables. Applied idempotently via ``CREATE … IF NOT EXISTS``, so
re-running on a populated DB is a no-op.

New columns must land via ALTER TABLE in a versioned migration: list them
in :data:`MIGRATIONS` (each is a ``(table, column, ddl)`` triple) and
:class:`~corlinman_evolution_store.store.EvolutionStore.open` will pragma-
check the column and apply the DDL only when missing.
"""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolution_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind   TEXT NOT NULL,
    target       TEXT,
    severity     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id     TEXT,
    session_id   TEXT,
    observed_at  INTEGER NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_evol_signals_kind_target
    ON evolution_signals(event_kind, target);

CREATE INDEX IF NOT EXISTS idx_evol_signals_observed
    ON evolution_signals(observed_at);

CREATE TABLE IF NOT EXISTS evolution_proposals (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL,
    target                TEXT NOT NULL,
    diff                  TEXT NOT NULL,
    reasoning             TEXT NOT NULL,
    risk                  TEXT NOT NULL,
    budget_cost           INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL,
    shadow_metrics        TEXT,
    eval_run_id           TEXT,
    baseline_metrics_json TEXT,
    signal_ids            TEXT NOT NULL,
    trace_ids             TEXT NOT NULL,
    created_at            INTEGER NOT NULL,
    decided_at            INTEGER,
    decided_by            TEXT,
    applied_at            INTEGER,
    auto_rollback_at      INTEGER,
    auto_rollback_reason  TEXT,
    metadata              TEXT,
    rollback_of           TEXT REFERENCES evolution_proposals(id),
    tenant_id             TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_evol_proposals_status
    ON evolution_proposals(status);

CREATE INDEX IF NOT EXISTS idx_evol_proposals_created
    ON evolution_proposals(created_at);

CREATE TABLE IF NOT EXISTS evolution_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id      TEXT NOT NULL REFERENCES evolution_proposals(id),
    kind             TEXT NOT NULL,
    target           TEXT NOT NULL,
    before_sha       TEXT NOT NULL,
    after_sha        TEXT NOT NULL,
    inverse_diff     TEXT NOT NULL,
    metrics_baseline TEXT NOT NULL,
    applied_at       INTEGER NOT NULL,
    rolled_back_at   INTEGER,
    rollback_reason  TEXT,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    share_with       TEXT
);

CREATE INDEX IF NOT EXISTS idx_evol_history_proposal
    ON evolution_history(proposal_id);

CREATE INDEX IF NOT EXISTS idx_evol_history_applied
    ON evolution_history(applied_at);

CREATE TABLE IF NOT EXISTS apply_intent_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    target          TEXT NOT NULL,
    intent_at       INTEGER NOT NULL,
    committed_at    INTEGER,
    failed_at       INTEGER,
    failure_reason  TEXT,
    tenant_id       TEXT NOT NULL DEFAULT 'default'
);
"""

# DDL applied *after* SCHEMA_SQL and the MIGRATIONS loop, so it can safely
# reference columns the migrations just added (e.g. ``tenant_id``).
POST_MIGRATIONS_SQL = """
CREATE INDEX IF NOT EXISTS idx_evol_signals_tenant_observed
    ON evolution_signals(tenant_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_evol_proposals_tenant_status
    ON evolution_proposals(tenant_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_evol_history_tenant_applied
    ON evolution_history(tenant_id, applied_at);

DROP INDEX IF EXISTS idx_apply_intent_uncommitted;

CREATE INDEX IF NOT EXISTS idx_apply_intent_tenant_uncommitted
    ON apply_intent_log(tenant_id, intent_at)
    WHERE committed_at IS NULL AND failed_at IS NULL;
"""

# Ordered list of (table, column, ddl) triples. Apply in array order;
# append-only. Mirrors ``MIGRATIONS`` in the Rust crate verbatim.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # v0.2 → v0.3 — ShadowTester adds eval_run_id + baseline_metrics_json.
    (
        "evolution_proposals",
        "eval_run_id",
        "ALTER TABLE evolution_proposals ADD COLUMN eval_run_id TEXT",
    ),
    (
        "evolution_proposals",
        "baseline_metrics_json",
        "ALTER TABLE evolution_proposals ADD COLUMN baseline_metrics_json TEXT",
    ),
    # Phase 3 W1-B AutoRollback audit trail.
    (
        "evolution_proposals",
        "auto_rollback_at",
        "ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_at INTEGER",
    ),
    (
        "evolution_proposals",
        "auto_rollback_reason",
        "ALTER TABLE evolution_proposals ADD COLUMN auto_rollback_reason TEXT",
    ),
    # Phase 4 W1 4-1A — tenant_id columns.
    (
        "evolution_signals",
        "tenant_id",
        "ALTER TABLE evolution_signals ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
    (
        "evolution_proposals",
        "tenant_id",
        "ALTER TABLE evolution_proposals ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
    (
        "evolution_history",
        "tenant_id",
        "ALTER TABLE evolution_history ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
    (
        "apply_intent_log",
        "tenant_id",
        "ALTER TABLE apply_intent_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    ),
    # Phase 4 W2 B1 iter 2 — proposal ``metadata`` blob.
    (
        "evolution_proposals",
        "metadata",
        "ALTER TABLE evolution_proposals ADD COLUMN metadata TEXT",
    ),
    # Phase 4 W2 B3 iter 3 — ``share_with`` on evolution_history.
    (
        "evolution_history",
        "share_with",
        "ALTER TABLE evolution_history ADD COLUMN share_with TEXT",
    ),
)


__all__ = ["MIGRATIONS", "POST_MIGRATIONS_SQL", "SCHEMA_SQL"]
