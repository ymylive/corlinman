//! Authoritative SQLite schema for the EvolutionLoop. Cross-language
//! contract — Python engine and Rust observer/API both bind to these tables.
//!
//! Applied idempotently via `CREATE … IF NOT EXISTS`, so re-running on a
//! populated DB is a no-op. New columns must land via ALTER TABLE in a
//! versioned migration: list them in [`MIGRATIONS`] (each is a
//! `(table, column, ddl)` triple) and `EvolutionStore::open` will pragma-check
//! the column and apply the DDL only when missing. Operator-facing notes for
//! each schema bump live in `docs/migration/`.

pub const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS evolution_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind   TEXT NOT NULL,
    target       TEXT,
    severity     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id     TEXT,
    session_id   TEXT,
    observed_at  INTEGER NOT NULL
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
    rollback_of           TEXT REFERENCES evolution_proposals(id)
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
    rollback_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_evol_history_proposal
    ON evolution_history(proposal_id);

CREATE INDEX IF NOT EXISTS idx_evol_history_applied
    ON evolution_history(applied_at);

-- Phase 3.1: forward-apply intent log.
--
-- `kb.sqlite` and `evolution.sqlite` are separate files (no cross-DB
-- transaction). The original Phase 2 ordering (kb mutate → evolution
-- TX) leaves a half-committed window if the gateway is killed between
-- the two writes: the kb is changed but no audit row exists, so the
-- monitor and any operator-facing UI both see an unchanged proposal.
-- The intent log writes one row *before* the kb mutation and stamps
-- `committed_at` (or `failed_at`) after. On gateway startup the scan
-- for rows where both stamps are NULL surfaces every half-committed
-- apply with enough information to manually reconcile — we
-- intentionally do **not** auto-restore: the operator decides whether
-- to retry forward, revert manually, or accept the kb state.
CREATE TABLE IF NOT EXISTS apply_intent_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    target          TEXT NOT NULL,
    intent_at       INTEGER NOT NULL,
    committed_at    INTEGER,
    failed_at       INTEGER,
    failure_reason  TEXT
);

-- Partial index only indexes uncommitted rows. The scan on startup is
-- the hot path; once a row is committed we never read it again so the
-- partial form keeps the index pages tiny on long-running deployments.
CREATE INDEX IF NOT EXISTS idx_apply_intent_uncommitted
    ON apply_intent_log(intent_at)
    WHERE committed_at IS NULL AND failed_at IS NULL;
"#;

/// One migration step: add `column` to `table` via `ddl` if it does not
/// already exist. Order matters — apply in array order. Append-only.
///
/// The store applies these *after* `SCHEMA_SQL`, so fresh DBs (which already
/// got the column via `CREATE TABLE`) hit the pragma check, see the column
/// is present, and skip the ALTER. Existing v0.2 DBs (which lack the
/// columns) get the ALTER and reach the same end state.
pub const MIGRATIONS: &[(&str, &str, &str)] = &[
    // v0.2 → v0.3 — Phase 3 W1-A ShadowTester adds eval_run_id +
    // baseline_metrics_json on `evolution_proposals` so the operator can
    // see the pre-/post- delta when reviewing a shadowed proposal. See
    // `docs/migration/v2-to-v3.md`.
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
    // Phase 3 W1-B AutoRollback adds the audit trail for proposals
    // that were auto-reverted: when the monitor decided + why (signal-
    // count delta string, threshold breached, etc). See
    // `docs/migration/v2-to-v3.md`.
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
];
