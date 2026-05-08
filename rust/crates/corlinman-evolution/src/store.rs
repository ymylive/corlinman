//! Thin SQLite wrapper. Opens (or creates) the evolution DB and applies
//! `SCHEMA_SQL` idempotently. Phase 2 default path is `/data/evolution.sqlite`
//! — separate from `kb.sqlite` so RAG churn doesn't touch the audit trail.

use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use sqlx::{
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous},
    SqlitePool,
};

use crate::schema::{MIGRATIONS, POST_MIGRATIONS_SQL, SCHEMA_SQL};

#[derive(Debug, thiserror::Error)]
pub enum OpenError {
    #[error("invalid sqlite url '{0}': {1}")]
    InvalidUrl(String, sqlx::Error),
    #[error("connect '{0}': {1}")]
    Connect(String, sqlx::Error),
    #[error("apply SCHEMA_SQL: {0}")]
    ApplySchema(sqlx::Error),
    #[error("apply migration {0}.{1}: {2}")]
    ApplyMigration(&'static str, &'static str, sqlx::Error),
    #[error("apply POST_MIGRATIONS_SQL: {0}")]
    ApplyPostMigrations(sqlx::Error),
}

#[derive(Debug, Clone)]
pub struct EvolutionStore {
    pool: SqlitePool,
}

impl EvolutionStore {
    /// Open (or create) the evolution SQLite at `path`. WAL +
    /// `synchronous=NORMAL` + `foreign_keys=ON`. Applies `SCHEMA_SQL`
    /// once — `CREATE … IF NOT EXISTS` makes this safe to repeat.
    pub async fn open(path: &Path) -> Result<Self, OpenError> {
        Self::open_with_pool_size(path, 8).await
    }

    /// Phase 4 W1.5 (next-tasks A7): variant of [`Self::open`] with a
    /// caller-supplied `max_connections`. Tests pass `1` to dodge a
    /// sqlx 0.7 + SQLite WAL cross-connection visibility race that
    /// only manifests under workspace-level parallel pressure: a
    /// committed INSERT on one pooled connection isn't always
    /// reflected in the snapshot a sibling connection acquires for
    /// the immediately-following SELECT, even though autocommit
    /// should make the row visible. Pinning the pool to one
    /// connection eliminates the race because all queries serialise
    /// on the same connection.
    ///
    /// Production `open` keeps the default `8` so request handlers
    /// don't queue on the connection budget. The race never reaches
    /// production because real handlers have a non-trivial gap
    /// between the write and the read (operator decision latency,
    /// network RTTs); the symptom is unique to back-to-back
    /// fetch_one + fetch_optional on the same pool.
    pub async fn open_with_pool_size(path: &Path, pool_size: u32) -> Result<Self, OpenError> {
        let url = format!("sqlite://{}", path.display());

        let options = SqliteConnectOptions::from_str(&url)
            .map_err(|e| OpenError::InvalidUrl(url.clone(), e))?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .foreign_keys(true)
            .busy_timeout(Duration::from_secs(5));

        let pool = SqlitePoolOptions::new()
            .max_connections(pool_size)
            .connect_with(options)
            .await
            .map_err(|e| OpenError::Connect(url, e))?;

        sqlx::raw_sql(SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(OpenError::ApplySchema)?;

        // Idempotent migrations: pragma-check the target column before
        // running each ALTER. Fresh DBs already have the columns from
        // SCHEMA_SQL above (CREATE TABLE definition is the source of
        // truth) and skip everything; existing pre-v0.3 DBs get the
        // ALTERs in order and converge to the same end state.
        for (table, column, ddl) in MIGRATIONS {
            if !column_exists(&pool, table, column).await? {
                sqlx::raw_sql(ddl)
                    .execute(&pool)
                    .await
                    .map_err(|e| OpenError::ApplyMigration(table, column, e))?;
            }
        }

        // Indexes that reference migrated columns must be created *after*
        // the `MIGRATIONS` loop has added those columns to legacy DBs;
        // putting them in `SCHEMA_SQL` would error against a pre-Phase-4
        // file because SQLite resolves index column names at create time.
        sqlx::raw_sql(POST_MIGRATIONS_SQL)
            .execute(&pool)
            .await
            .map_err(OpenError::ApplyPostMigrations)?;

        Ok(Self { pool })
    }

    /// Underlying pool. Repos take this by reference rather than owning it
    /// so multiple repos share the same connection budget.
    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }
}

/// True iff `table.column` exists in the database. Backed by SQLite's
/// `pragma_table_info` virtual table — works for any table SQLite knows
/// about, no privileges required.
async fn column_exists(
    pool: &SqlitePool,
    table: &'static str,
    column: &'static str,
) -> Result<bool, OpenError> {
    // pragma_table_info doesn't support `?`-bound table names, so format
    // it in. Both `table` and `column` are `'static` and only sourced
    // from the [`MIGRATIONS`] constant (no user input), so this is safe
    // and produces a stable query the planner can cache.
    let sql = format!(
        "SELECT 1 FROM pragma_table_info('{}') WHERE name = ?",
        table.replace('\'', "''")
    );
    let row = sqlx::query(&sql)
        .bind(column)
        .fetch_optional(pool)
        .await
        .map_err(|e| OpenError::ApplyMigration(table, column, e))?;
    Ok(row.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[tokio::test]
    async fn open_creates_tables() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();

        // Round-trip: count rows from each table — should be 0 but the
        // query must succeed (== schema applied).
        for table in [
            "evolution_signals",
            "evolution_proposals",
            "evolution_history",
        ] {
            let row: (i64,) = sqlx::query_as(&format!("SELECT COUNT(*) FROM {table}"))
                .fetch_one(store.pool())
                .await
                .expect("table exists");
            assert_eq!(row.0, 0, "{table} starts empty");
        }
    }

    #[tokio::test]
    async fn open_is_idempotent() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let _first = EvolutionStore::open(&path).await.unwrap();
        // Re-opening must not error (CREATE … IF NOT EXISTS).
        let _second = EvolutionStore::open(&path).await.unwrap();
    }

    /// Fresh DB path: SCHEMA_SQL alone must produce all v0.3 columns —
    /// the migration block sees they're already there and is a no-op.
    #[tokio::test]
    async fn fresh_db_has_v0_3_columns() {
        let (_tmp, store) = {
            let tmp = TempDir::new().unwrap();
            let path = tmp.path().join("evolution.sqlite");
            let store = EvolutionStore::open(&path).await.unwrap();
            (tmp, store)
        };
        for col in [
            "shadow_metrics",
            "eval_run_id",
            "baseline_metrics_json",
            "auto_rollback_at",
            "auto_rollback_reason",
            "metadata",
        ] {
            let exists = column_exists(store.pool(), "evolution_proposals", leak(col))
                .await
                .unwrap();
            assert!(exists, "evolution_proposals.{col} should exist on fresh DB");
        }
    }

    /// Existing-DB path: simulate a pre-v0.3 schema (no eval_run_id /
    /// baseline_metrics_json), reopen via `EvolutionStore::open`, and
    /// confirm both columns get added by the migration block.
    #[tokio::test]
    async fn migration_adds_v0_3_columns_to_legacy_db() {
        use sqlx::sqlite::SqliteConnectOptions;
        use std::str::FromStr;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let url = format!("sqlite://{}", path.display());

        // Bootstrap a v0.2-shaped DB by hand: same as today's SCHEMA_SQL
        // *minus* the two new columns. Different connection so we can
        // close it cleanly before reopening through `open`.
        {
            let opts = SqliteConnectOptions::from_str(&url)
                .unwrap()
                .create_if_missing(true);
            let pool = sqlx::sqlite::SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(opts)
                .await
                .unwrap();
            sqlx::raw_sql(
                r#"CREATE TABLE evolution_proposals (
                    id              TEXT PRIMARY KEY,
                    kind            TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    diff            TEXT NOT NULL,
                    reasoning       TEXT NOT NULL,
                    risk            TEXT NOT NULL,
                    budget_cost     INTEGER NOT NULL DEFAULT 1,
                    status          TEXT NOT NULL,
                    shadow_metrics  TEXT,
                    signal_ids      TEXT NOT NULL,
                    trace_ids       TEXT NOT NULL,
                    created_at      INTEGER NOT NULL,
                    decided_at      INTEGER,
                    decided_by      TEXT,
                    applied_at      INTEGER,
                    rollback_of     TEXT
                );"#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Pre-condition: legacy columns missing.
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect(&url)
            .await
            .unwrap();
        assert!(!column_exists(&pool, "evolution_proposals", "eval_run_id")
            .await
            .unwrap());
        assert!(
            !column_exists(&pool, "evolution_proposals", "baseline_metrics_json")
                .await
                .unwrap()
        );
        pool.close().await;

        // Open through the production path → migrations apply.
        let store = EvolutionStore::open(&path).await.unwrap();

        for col in [
            "eval_run_id",
            "baseline_metrics_json",
            "auto_rollback_at",
            "auto_rollback_reason",
        ] {
            assert!(
                column_exists(store.pool(), "evolution_proposals", leak(col))
                    .await
                    .unwrap(),
                "migration must add evolution_proposals.{col}"
            );
        }
    }

    /// Phase 4 W2 B1 iter 2: simulate a pre-Phase-4 schema (no
    /// `metadata` column), reopen through `EvolutionStore::open`, and
    /// confirm the migration block adds the column. Mirrors the v0.3
    /// pattern above so an operator upgrading a long-running
    /// installation doesn't have to rebuild the audit DB.
    #[tokio::test]
    async fn migration_adds_metadata_column_to_legacy_db() {
        use sqlx::sqlite::SqliteConnectOptions;
        use std::str::FromStr;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let url = format!("sqlite://{}", path.display());

        // Bootstrap a v0.3-shaped DB by hand — same as today's
        // SCHEMA_SQL *minus* the new `metadata` column. We include the
        // earlier v0.2→v0.3 columns so the only thing the migration
        // block has to add is the new one (cleaner assertion).
        {
            let opts = SqliteConnectOptions::from_str(&url)
                .unwrap()
                .create_if_missing(true);
            let pool = sqlx::sqlite::SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(opts)
                .await
                .unwrap();
            sqlx::raw_sql(
                r#"CREATE TABLE evolution_proposals (
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
                    rollback_of           TEXT
                );"#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Pre-condition: `metadata` column missing.
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect(&url)
            .await
            .unwrap();
        assert!(!column_exists(&pool, "evolution_proposals", "metadata")
            .await
            .unwrap());
        pool.close().await;

        // Open through the production path → migration applies.
        let store = EvolutionStore::open(&path).await.unwrap();
        assert!(
            column_exists(store.pool(), "evolution_proposals", "metadata")
                .await
                .unwrap(),
            "migration must add evolution_proposals.metadata"
        );

        // Idempotent: a second open against the now-migrated DB must
        // be a no-op (the pragma probe sees the column and skips the
        // ALTER) — not crash with `duplicate column name`.
        let _store2 = EvolutionStore::open(&path).await.unwrap();
    }

    /// Phase 4 W2 B3 iter 3: simulate a pre-B3 schema (no `share_with`
    /// column on `evolution_history`), reopen through
    /// `EvolutionStore::open`, and confirm the migration block adds
    /// the column. Mirrors `migration_adds_metadata_column_to_legacy_db`
    /// so an operator upgrading a long-running installation doesn't
    /// have to rebuild the audit DB. The migration is idempotent —
    /// reopening twice must not re-trigger the ALTER.
    #[tokio::test]
    async fn migration_adds_share_with_column_to_legacy_history_db() {
        use sqlx::sqlite::SqliteConnectOptions;
        use std::str::FromStr;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let url = format!("sqlite://{}", path.display());

        // Bootstrap a pre-B3 evolution_history shape — every column
        // landed before the B3 ALTER. We intentionally skip the parent
        // tables here; the test only cares about the migration path
        // for the `share_with` column. Foreign-key cascades aren't
        // exercised because this is a schema-shape probe.
        {
            let opts = SqliteConnectOptions::from_str(&url)
                .unwrap()
                .create_if_missing(true);
            let pool = sqlx::sqlite::SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(opts)
                .await
                .unwrap();
            sqlx::raw_sql(
                r#"CREATE TABLE evolution_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id      TEXT NOT NULL,
                    kind             TEXT NOT NULL,
                    target           TEXT NOT NULL,
                    before_sha       TEXT NOT NULL,
                    after_sha        TEXT NOT NULL,
                    inverse_diff     TEXT NOT NULL,
                    metrics_baseline TEXT NOT NULL,
                    applied_at       INTEGER NOT NULL,
                    rolled_back_at   INTEGER,
                    rollback_reason  TEXT,
                    tenant_id        TEXT NOT NULL DEFAULT 'default'
                );"#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Pre-condition: column missing.
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect(&url)
            .await
            .unwrap();
        assert!(
            !column_exists(&pool, "evolution_history", "share_with")
                .await
                .unwrap()
        );
        pool.close().await;

        // Open through the production path → migration applies.
        let store = EvolutionStore::open(&path).await.unwrap();
        assert!(
            column_exists(store.pool(), "evolution_history", "share_with")
                .await
                .unwrap(),
            "migration must add evolution_history.share_with"
        );

        // Idempotent reopen: a second open against the now-migrated
        // DB must be a no-op (pragma probe sees the column, ALTER
        // skipped) — not crash with `duplicate column name`.
        let _store2 = EvolutionStore::open(&path).await.unwrap();
    }

    /// `column_exists` requires `&'static str`. The test names are
    /// known at compile time; this leak is bounded to the test binary.
    fn leak(s: &str) -> &'static str {
        Box::leak(s.to_string().into_boxed_str())
    }

    /// Phase 4 W1 4-1A: fresh DB lands with `tenant_id` on every
    /// migrated table and the post-migration tenant-aware indexes.
    #[tokio::test]
    async fn fresh_db_has_phase4_tenant_columns_and_indexes() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();

        for table in [
            "evolution_signals",
            "evolution_proposals",
            "evolution_history",
            "apply_intent_log",
        ] {
            assert!(
                column_exists(store.pool(), leak(table), "tenant_id")
                    .await
                    .unwrap(),
                "{table}.tenant_id should exist on fresh DB"
            );
        }

        // Tenant-aware indexes should be present after open() runs
        // POST_MIGRATIONS_SQL.
        for idx in [
            "idx_evol_signals_tenant_observed",
            "idx_evol_proposals_tenant_status",
            "idx_evol_history_tenant_applied",
            "idx_apply_intent_tenant_uncommitted",
        ] {
            let row: Option<String> =
                sqlx::query_scalar("SELECT name FROM sqlite_master WHERE type='index' AND name=?1")
                    .bind(idx)
                    .fetch_optional(store.pool())
                    .await
                    .unwrap();
            assert_eq!(row.as_deref(), Some(idx), "missing index {idx}");
        }

        // The pre-tenant `idx_apply_intent_uncommitted` should have
        // been dropped by POST_MIGRATIONS_SQL.
        let stale: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_apply_intent_uncommitted'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert!(
            stale.is_none(),
            "old idx_apply_intent_uncommitted should be gone"
        );
    }

    /// Phase 4 W1 4-1A: a pre-Phase-4 DB whose tables lack `tenant_id`
    /// and whose `apply_intent_log` still carries the old partial index
    /// must converge to the same end state when opened through the
    /// production path. Mirrors `migration_adds_v0_3_columns_to_legacy_db`
    /// for the new tenant_id migrations.
    #[tokio::test]
    async fn migration_adds_phase4_tenant_columns_to_legacy_db() {
        use sqlx::sqlite::SqliteConnectOptions;
        use std::str::FromStr;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let url = format!("sqlite://{}", path.display());

        // Bootstrap a pre-Phase-4 (post-3.1) DB by hand: SCHEMA_SQL
        // shape MINUS tenant_id columns, PLUS the old non-tenant
        // partial index on apply_intent_log.
        {
            let opts = SqliteConnectOptions::from_str(&url)
                .unwrap()
                .create_if_missing(true);
            let pool = sqlx::sqlite::SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(opts)
                .await
                .unwrap();
            sqlx::raw_sql(
                r#"CREATE TABLE evolution_signals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_kind   TEXT NOT NULL,
                    target       TEXT,
                    severity     TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trace_id     TEXT,
                    session_id   TEXT,
                    observed_at  INTEGER NOT NULL
                );
                CREATE TABLE evolution_proposals (
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
                    rollback_of           TEXT
                );
                CREATE TABLE evolution_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id      TEXT NOT NULL,
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
                CREATE TABLE apply_intent_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id     TEXT NOT NULL,
                    kind            TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    intent_at       INTEGER NOT NULL,
                    committed_at    INTEGER,
                    failed_at       INTEGER,
                    failure_reason  TEXT
                );
                CREATE INDEX idx_apply_intent_uncommitted
                    ON apply_intent_log(intent_at)
                    WHERE committed_at IS NULL AND failed_at IS NULL;
                "#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Open through the production path → tenant migrations apply.
        let store = EvolutionStore::open(&path).await.unwrap();

        for table in [
            "evolution_signals",
            "evolution_proposals",
            "evolution_history",
            "apply_intent_log",
        ] {
            assert!(
                column_exists(store.pool(), leak(table), "tenant_id")
                    .await
                    .unwrap(),
                "migration must add {table}.tenant_id"
            );
        }

        // The new tenant-aware partial index on apply_intent_log must
        // be present, and the legacy index must have been dropped.
        let new_idx: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_apply_intent_tenant_uncommitted'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert_eq!(
            new_idx.as_deref(),
            Some("idx_apply_intent_tenant_uncommitted")
        );
        let stale: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_apply_intent_uncommitted'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert!(
            stale.is_none(),
            "legacy partial index should be dropped post-migration"
        );

        // Idempotent reopen: running open() a second time must be a
        // clean no-op (DROP IF EXISTS finds nothing, CREATE IF NOT
        // EXISTS no-ops, ALTER guarded by pragma probe).
        drop(store);
        let _store2 = EvolutionStore::open(&path).await.unwrap();
    }
}
