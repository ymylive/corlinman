//! Schema migrations for the corlinman SQLite store.
//!
//! Historically this module was an `if current == 1 { … } if current == 2 { … }`
//! ladder hard-coded against one DB. Sprint 3 T2 turned it into a
//! trait-based registry so adding a new step is "write a struct, register
//! it" instead of "edit the ladder and pray".
//!
//! Shipped steps: [`V1ToV2FtsBackfill`], [`V2ToV3PendingApprovals`],
//! [`V3ToV4ChunkTags`]. [`MigrationRegistry::target_version`] is `4`.
//!
//! # Architecture
//!
//! - [`MigrationScript`] — one forward (`up`) + one best-effort rollback
//!   (`down`) step, both executed **inside a sqlx transaction** so a
//!   partial failure never leaks half-applied DDL.
//! - [`MigrationRegistry`] — an ordered list of scripts. `builtin()`
//!   returns the shipped set; callers can `register()` extra steps
//!   (useful for tests that want to inject a deliberately broken step
//!   to exercise the rollback path).
//! - [`MigrationRegistry::migrate_up`] walks `current → target_version()`
//!   one script at a time, bumping `kv_store.schema_version` inside the
//!   same transaction as the DDL; if anything fails mid-sprint, the
//!   transaction rolls back and the stored version stays at the last
//!   successful step.
//! - [`MigrationRegistry::migrate_down_to`] does the same in reverse
//!   (scripts declare `down()` semantics individually; v1→v2 is
//!   intentionally irreversible and returns [`CorlinmanError::Storage`]).
//!
//! `ensure_schema` remains as a thin adapter so the three existing
//! callers (gateway middleware, admin routes, integration tests) keep
//! compiling without touching their imports.
//!
//! # TODO
//!
//! - `CorlinmanError::Unsupported` variant — currently we reuse
//!   `Storage("down not supported…")` because the enum in
//!   `corlinman-core` doesn't yet have a dedicated "unsupported
//!   operation" case. Plumbing it through would touch another crate.
//! - Concurrent-boot migration lock — today we rely on SQLite's own
//!   transaction serialisation; an advisory `kv_store('migration_lock')`
//!   row would let us fail fast if two processes race a cold start.

use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use corlinman_core::error::CorlinmanError;
use sqlx::{Row, Sqlite, SqlitePool, Transaction};

use crate::header::probe_and_convert_if_needed;
use crate::sqlite::SqliteStore;

/// Type alias kept to match the task spec's trait signature. sqlx's
/// actual transaction type is `sqlx::Transaction<'a, sqlx::Sqlite>` —
/// exporting this name keeps migration authors from having to import
/// two sqlx modules.
pub type SqliteTransaction<'a> = Transaction<'a, Sqlite>;

/// A single forward / rollback migration step.
///
/// Implementors **must** write only inside the provided transaction; the
/// registry commits it after `up`/`down` returns `Ok(())`. Committing or
/// rolling back the transaction manually is a protocol error.
#[async_trait]
pub trait MigrationScript: Send + Sync {
    /// Schema version the script expects to find before running `up`.
    fn from(&self) -> u32;
    /// Schema version the script installs on success.
    fn to(&self) -> u32;
    /// Short human-readable id (used in [`MigrationReport::scripts_applied`]).
    fn name(&self) -> &'static str;

    /// Forward migration, run inside a per-script transaction.
    async fn up(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError>;

    /// Rollback. Some migrations are intentionally irreversible — those
    /// should return [`CorlinmanError::Storage`] with a message that
    /// starts with `"down not supported"` so the registry can surface a
    /// clear diagnostic without swallowing the intent.
    async fn down(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError>;
}

/// Outcome of running [`MigrationRegistry::migrate_up`] or
/// [`MigrationRegistry::migrate_down_to`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrationReport {
    pub from: u32,
    pub to: u32,
    pub scripts_applied: Vec<String>,
}

/// Ordered collection of migration scripts.
pub struct MigrationRegistry {
    scripts: Vec<Arc<dyn MigrationScript>>,
}

impl MigrationRegistry {
    /// Empty registry — primarily useful in tests that want full control.
    pub fn new() -> Self {
        Self {
            scripts: Vec::new(),
        }
    }

    /// The scripts shipped by corlinman-vector today: v1→v2 (FTS5
    /// backfill), v2→v3 (pending_approvals), v3→v4 (chunk_tags).
    pub fn builtin() -> Self {
        Self {
            scripts: vec![
                Arc::new(V1ToV2FtsBackfill) as Arc<dyn MigrationScript>,
                Arc::new(V2ToV3PendingApprovals) as Arc<dyn MigrationScript>,
                Arc::new(V3ToV4ChunkTags) as Arc<dyn MigrationScript>,
            ],
        }
    }

    /// Append a script. Order is preserved — `migrate_up` walks scripts
    /// in insertion order looking for the one whose `from()` matches the
    /// current stored version.
    pub fn register(&mut self, script: Arc<dyn MigrationScript>) {
        self.scripts.push(script);
    }

    /// The highest `to()` version in the registry — what a fully-applied
    /// DB should report. Returns 0 for an empty registry.
    pub fn target_version(&self) -> u32 {
        self.scripts.iter().map(|s| s.to()).max().unwrap_or(0)
    }

    /// Walk forward from the current stored version to `target_version()`.
    ///
    /// Each script runs in its own transaction, which is committed only
    /// after both the DDL and the `schema_version` bump succeed. A
    /// failure rolls the per-script transaction back and returns the
    /// error — earlier scripts stay committed, so the DB ends up at the
    /// last successful step rather than in a half-applied state.
    pub async fn migrate_up(&self, pool: &SqlitePool) -> Result<MigrationReport, CorlinmanError> {
        let start = read_schema_version(pool).await?;
        let target = self.target_version() as i64;
        let mut current = start;
        let mut applied = Vec::new();

        if start > target {
            return Err(CorlinmanError::Config(format!(
                "schema_version={start} is newer than registry target={target}; \
                 refusing to auto-downgrade"
            )));
        }

        while current < target {
            let script = self
                .scripts
                .iter()
                .find(|s| s.from() as i64 == current)
                .ok_or_else(|| {
                    CorlinmanError::Config(format!(
                        "no migration script registered for from={current}"
                    ))
                })?;
            run_up_in_tx(pool, script.as_ref()).await?;
            applied.push(script.name().to_string());
            current = script.to() as i64;
        }

        Ok(MigrationReport {
            from: start as u32,
            to: current as u32,
            scripts_applied: applied,
        })
    }

    /// Walk backwards from the current stored version to `target`.
    ///
    /// Each `down()` runs in its own transaction; the first
    /// irreversible step aborts with the script's own error and leaves
    /// the DB at that intermediate version.
    pub async fn migrate_down_to(
        &self,
        pool: &SqlitePool,
        target: u32,
    ) -> Result<MigrationReport, CorlinmanError> {
        let start = read_schema_version(pool).await?;
        let mut current = start;
        let mut applied = Vec::new();

        if (target as i64) > start {
            return Err(CorlinmanError::Config(format!(
                "migrate_down_to target={target} is above current={start}"
            )));
        }

        while current > target as i64 {
            let script = self
                .scripts
                .iter()
                .find(|s| s.to() as i64 == current)
                .ok_or_else(|| {
                    CorlinmanError::Config(format!(
                        "no migration script registered that produces version={current}"
                    ))
                })?;
            run_down_in_tx(pool, script.as_ref()).await?;
            applied.push(script.name().to_string());
            current = script.from() as i64;
        }

        Ok(MigrationReport {
            from: start as u32,
            to: current as u32,
            scripts_applied: applied,
        })
    }
}

impl Default for MigrationRegistry {
    fn default() -> Self {
        Self::builtin()
    }
}

async fn run_up_in_tx(
    pool: &SqlitePool,
    script: &dyn MigrationScript,
) -> Result<(), CorlinmanError> {
    let mut tx = pool
        .begin()
        .await
        .map_err(|e| CorlinmanError::Storage(format!("begin tx for {}: {e}", script.name())))?;
    script.up(&mut tx).await?;
    set_schema_version_in_tx(&mut tx, script.to() as i64).await?;
    tx.commit().await.map_err(|e| {
        CorlinmanError::Storage(format!("commit tx for {} (up): {e}", script.name()))
    })?;
    Ok(())
}

async fn run_down_in_tx(
    pool: &SqlitePool,
    script: &dyn MigrationScript,
) -> Result<(), CorlinmanError> {
    let mut tx = pool
        .begin()
        .await
        .map_err(|e| CorlinmanError::Storage(format!("begin tx for {}: {e}", script.name())))?;
    script.down(&mut tx).await?;
    set_schema_version_in_tx(&mut tx, script.from() as i64).await?;
    tx.commit().await.map_err(|e| {
        CorlinmanError::Storage(format!("commit tx for {} (down): {e}", script.name()))
    })?;
    Ok(())
}

async fn read_schema_version(pool: &SqlitePool) -> Result<i64, CorlinmanError> {
    let row = sqlx::query("SELECT value FROM kv_store WHERE key = 'schema_version'")
        .fetch_optional(pool)
        .await
        .map_err(|e| CorlinmanError::Storage(format!("read schema_version: {e}")))?;
    match row {
        None => Ok(0),
        Some(r) => {
            let raw: Option<String> = r.get("value");
            match raw {
                None => Ok(0),
                Some(s) => s.parse::<i64>().map_err(|e| {
                    CorlinmanError::Config(format!("schema_version='{s}' is not an integer: {e}"))
                }),
            }
        }
    }
}

async fn set_schema_version_in_tx(
    tx: &mut SqliteTransaction<'_>,
    v: i64,
) -> Result<(), CorlinmanError> {
    sqlx::query(
        "INSERT OR REPLACE INTO kv_store(key, value, vector) VALUES ('schema_version', ?1, NULL)",
    )
    .bind(v.to_string())
    .execute(&mut **tx)
    .await
    .map_err(|e| CorlinmanError::Storage(format!("write schema_version={v}: {e}")))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Shipped migrations
// ---------------------------------------------------------------------------

/// v1 → v2: repopulate the FTS5 virtual table from existing `chunks`
/// rows. The table + triggers are created by `SCHEMA_SQL` (idempotent),
/// so this just issues the one-shot `rebuild` command; pre-existing
/// rows that inserted before the triggers existed become searchable.
///
/// `down()` is intentionally unsupported: dropping `chunks_fts` without
/// losing the triggers (and vice-versa) is fragile, and nobody has asked
/// for a v2 → v1 path.
pub struct V1ToV2FtsBackfill;

#[async_trait]
impl MigrationScript for V1ToV2FtsBackfill {
    fn from(&self) -> u32 {
        1
    }
    fn to(&self) -> u32 {
        2
    }
    fn name(&self) -> &'static str {
        "v1_to_v2_fts_backfill"
    }

    async fn up(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        sqlx::query("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v1→v2 rebuild chunks_fts: {e}")))?;
        Ok(())
    }

    async fn down(&self, _tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        Err(CorlinmanError::Storage(
            "down not supported for v1→v2 fts rebuild".into(),
        ))
    }
}

/// v2 → v3: materialise the `pending_approvals` table used by the
/// gateway's approval gate (Sprint 2 T3). The DDL lives in
/// `SCHEMA_SQL` with `IF NOT EXISTS`, so `up()` is a no-op on a fresh
/// file; for legacy v2 DBs it's still a no-op because `SqliteStore::open`
/// already ran the schema script before the migration starts.
///
/// `down()` drops the table and its two indexes so tests can exercise
/// the round-trip path.
pub struct V2ToV3PendingApprovals;

#[async_trait]
impl MigrationScript for V2ToV3PendingApprovals {
    fn from(&self) -> u32 {
        2
    }
    fn to(&self) -> u32 {
        3
    }
    fn name(&self) -> &'static str {
        "v2_to_v3_pending_approvals"
    }

    async fn up(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        // Defensive re-create: SCHEMA_SQL already ran during open(), so
        // this is an IF NOT EXISTS no-op in production, but it makes
        // `up()` self-sufficient in tests that bypass open().
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS pending_approvals (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                plugin TEXT NOT NULL,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                decision TEXT
            )",
        )
        .execute(&mut **tx)
        .await
        .map_err(|e| CorlinmanError::Storage(format!("v2→v3 create pending_approvals: {e}")))?;
        Ok(())
    }

    async fn down(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        sqlx::query("DROP INDEX IF EXISTS idx_pending_approvals_undecided")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v3→v2 drop undecided idx: {e}")))?;
        sqlx::query("DROP INDEX IF EXISTS idx_pending_approvals_requested")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v3→v2 drop requested idx: {e}")))?;
        sqlx::query("DROP TABLE IF EXISTS pending_approvals")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v3→v2 drop pending_approvals: {e}")))?;
        Ok(())
    }
}

/// v3 → v4: materialise the `chunk_tags` many-to-many table plus
/// `idx_chunk_tags_tag`, introduced with the Sprint 3 T4 tag-filter
/// pushdown. Like the v2→v3 step this is mostly a no-op in production
/// because `SCHEMA_SQL` declares the DDL `IF NOT EXISTS`; the explicit
/// `up()` is kept so the script is self-contained for tests that bypass
/// [`SqliteStore::open`] and so the intent survives future schema
/// churn.
///
/// `down()` drops the index and table; it does **not** touch the parent
/// `chunks` rows (which would stay intact without their tag annotations).
pub struct V3ToV4ChunkTags;

#[async_trait]
impl MigrationScript for V3ToV4ChunkTags {
    fn from(&self) -> u32 {
        3
    }
    fn to(&self) -> u32 {
        4
    }
    fn name(&self) -> &'static str {
        "v3_to_v4_chunk_tags"
    }

    async fn up(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS chunk_tags (
                chunk_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (chunk_id, tag),
                FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            )",
        )
        .execute(&mut **tx)
        .await
        .map_err(|e| CorlinmanError::Storage(format!("v3→v4 create chunk_tags: {e}")))?;
        sqlx::query("CREATE INDEX IF NOT EXISTS idx_chunk_tags_tag ON chunk_tags(tag)")
            .execute(&mut **tx)
            .await
            .map_err(|e| {
                CorlinmanError::Storage(format!("v3→v4 create idx_chunk_tags_tag: {e}"))
            })?;
        Ok(())
    }

    async fn down(&self, tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
        sqlx::query("DROP INDEX IF EXISTS idx_chunk_tags_tag")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v4→v3 drop idx_chunk_tags_tag: {e}")))?;
        sqlx::query("DROP TABLE IF EXISTS chunk_tags")
            .execute(&mut **tx)
            .await
            .map_err(|e| CorlinmanError::Storage(format!("v4→v3 drop chunk_tags: {e}")))?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Compatibility adapter — keeps the pre-Sprint-3 API working.
// ---------------------------------------------------------------------------

/// Outcome of [`ensure_schema`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MigrationOutcome {
    /// `kv_store` had no `schema_version`; we wrote the current one.
    Initialised(i64),
    /// `schema_version` matched [`crate::SCHEMA_VERSION`] — nothing to do.
    UpToDate(i64),
    /// One or more migrations ran. Payload: `(from, to)`.
    Migrated { from: i64, to: i64 },
}

/// Bootstrap / verify `schema_version` using the built-in registry.
///
/// Retained as-is for existing callers (gateway middleware, admin
/// routes, integration tests). Internally it delegates to
/// [`MigrationRegistry::builtin`] + [`MigrationRegistry::migrate_up`].
pub async fn ensure_schema(store: &SqliteStore) -> Result<MigrationOutcome, CorlinmanError> {
    let pool = store.pool();
    let before = read_schema_version(pool).await?;
    let registry = MigrationRegistry::builtin();
    let target = registry.target_version() as i64;

    if before == 0 {
        // Fresh DB — stamp the version directly, no scripts to apply.
        let mut tx = pool
            .begin()
            .await
            .map_err(|e| CorlinmanError::Storage(format!("begin tx for init: {e}")))?;
        set_schema_version_in_tx(&mut tx, target).await?;
        tx.commit()
            .await
            .map_err(|e| CorlinmanError::Storage(format!("commit init: {e}")))?;
        return Ok(MigrationOutcome::Initialised(target));
    }
    if before == target {
        return Ok(MigrationOutcome::UpToDate(target));
    }
    if before < 1 || before > target {
        return Err(CorlinmanError::Config(format!(
            "schema_version mismatch: stored={before} current={target}; no migration path"
        )));
    }

    let report = registry.migrate_up(pool).await?;
    Ok(MigrationOutcome::Migrated {
        from: report.from as i64,
        to: report.to as i64,
    })
}

/// Convenience wrapper re-exported for callers that want the public
/// `probe_and_convert_if_needed` under `migration::`.
pub async fn probe_index_header(
    index_path: &Path,
    expected_dim: usize,
) -> Result<(), CorlinmanError> {
    probe_and_convert_if_needed(index_path, expected_dim)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    async fn fresh_store() -> (SqliteStore, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        (store, tmp)
    }

    /// Helper: drop any existing schema_version row so the DB looks like
    /// it came from a time before migrations were recorded.
    async fn wipe_schema_version(store: &SqliteStore) {
        sqlx::query("DELETE FROM kv_store WHERE key = 'schema_version'")
            .execute(store.pool())
            .await
            .unwrap();
    }

    // -- 1. empty_db_migrates_from_0_to_target -----------------------------

    #[tokio::test]
    async fn empty_db_migrates_from_0_to_target() {
        let (store, _tmp) = fresh_store().await;
        wipe_schema_version(&store).await;
        // Also simulate a "v1" DB where the chunks table has content
        // inserted before FTS5 triggers existed.
        let file_id = store.insert_file("d.md", "d", "h", 0, 0).await.unwrap();
        store
            .insert_chunk(file_id, 0, "alpha bravo charlie", None)
            .await
            .unwrap();
        // The schema_version is missing → 0. Use ensure_schema's init
        // path on fresh DB, OR call migrate_up after stamping v1.
        store.kv_set("schema_version", "1").await.unwrap();

        let registry = MigrationRegistry::builtin();
        assert_eq!(registry.target_version(), 4);
        let report = registry.migrate_up(store.pool()).await.unwrap();
        assert_eq!(report.from, 1);
        assert_eq!(report.to, 4);
        assert_eq!(report.scripts_applied.len(), 3);
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("4")
        );
        // Tables the registry should have left in place:
        for t in [
            "files",
            "chunks",
            "kv_store",
            "chunks_fts",
            "pending_approvals",
            "chunk_tags",
        ] {
            assert!(store.table_exists(t).await.unwrap(), "missing table {t}");
        }
    }

    // -- 2. idempotent_rerun ------------------------------------------------

    #[tokio::test]
    async fn idempotent_rerun() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "1").await.unwrap();
        let registry = MigrationRegistry::builtin();
        let first = registry.migrate_up(store.pool()).await.unwrap();
        assert_eq!(first.to, 4);
        assert!(!first.scripts_applied.is_empty());

        let second = registry.migrate_up(store.pool()).await.unwrap();
        assert_eq!(second.from, 4);
        assert_eq!(second.to, 4);
        assert!(second.scripts_applied.is_empty());
    }

    // -- 3. v3_down_drops_pending_approvals --------------------------------

    #[tokio::test]
    async fn v3_down_drops_pending_approvals() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "3").await.unwrap();
        assert!(store.table_exists("pending_approvals").await.unwrap());

        let registry = MigrationRegistry::builtin();
        let report = registry.migrate_down_to(store.pool(), 2).await.unwrap();
        assert_eq!(report.from, 3);
        assert_eq!(report.to, 2);
        assert_eq!(report.scripts_applied.len(), 1);
        assert_eq!(report.scripts_applied[0], "v2_to_v3_pending_approvals");
        assert!(!store.table_exists("pending_approvals").await.unwrap());
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("2")
        );
    }

    // -- 3b. v4_down_drops_chunk_tags --------------------------------------

    #[tokio::test]
    async fn v4_down_drops_chunk_tags() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "4").await.unwrap();
        assert!(store.table_exists("chunk_tags").await.unwrap());

        let registry = MigrationRegistry::builtin();
        let report = registry.migrate_down_to(store.pool(), 3).await.unwrap();
        assert_eq!(report.from, 4);
        assert_eq!(report.to, 3);
        assert_eq!(report.scripts_applied.len(), 1);
        assert_eq!(report.scripts_applied[0], "v3_to_v4_chunk_tags");
        assert!(!store.table_exists("chunk_tags").await.unwrap());
        // Parent table untouched.
        assert!(store.table_exists("chunks").await.unwrap());
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("3")
        );
    }

    // -- 4. v1_down_unsupported_returns_err --------------------------------

    #[tokio::test]
    async fn v1_down_unsupported_returns_err() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "2").await.unwrap();
        let registry = MigrationRegistry::builtin();
        let err = registry.migrate_down_to(store.pool(), 1).await.unwrap_err();
        match &err {
            CorlinmanError::Storage(msg) => {
                assert!(msg.contains("down not supported"), "unexpected msg: {msg}");
            }
            other => panic!("expected Storage, got {other:?}"),
        }
        // Version stayed put — the failing down() rolled back.
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("2")
        );
    }

    // -- 5. partial_failure_rolls_back -------------------------------------

    struct AlwaysFails;
    #[async_trait]
    impl MigrationScript for AlwaysFails {
        fn from(&self) -> u32 {
            4
        }
        fn to(&self) -> u32 {
            5
        }
        fn name(&self) -> &'static str {
            "always_fails"
        }
        async fn up(&self, _tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
            Err(CorlinmanError::Storage("synthetic failure".into()))
        }
        async fn down(&self, _tx: &mut SqliteTransaction<'_>) -> Result<(), CorlinmanError> {
            Ok(())
        }
    }

    #[tokio::test]
    async fn partial_failure_rolls_back() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "1").await.unwrap();

        let mut registry = MigrationRegistry::builtin();
        registry.register(Arc::new(AlwaysFails));
        assert_eq!(registry.target_version(), 5);

        let err = registry.migrate_up(store.pool()).await.unwrap_err();
        assert!(err.to_string().contains("synthetic failure"));

        // v1→v2, v2→v3, v3→v4 committed successfully; v4→v5 failed and
        // its transaction was rolled back, so we're parked at v4.
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("4"),
            "should be parked at last successful step"
        );
        assert!(store.table_exists("pending_approvals").await.unwrap());
        assert!(store.table_exists("chunk_tags").await.unwrap());
    }

    // -- 6. migrate_down_to_v2_then_up_to_4 --------------------------------

    #[tokio::test]
    async fn migrate_down_to_v2_then_up_to_4() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "4").await.unwrap();

        let registry = MigrationRegistry::builtin();
        // v4 → v3 → v2 (v1→v2 is irreversible, so 2 is the floor we
        // can reach with down()).
        let down = registry.migrate_down_to(store.pool(), 2).await.unwrap();
        assert_eq!(down.from, 4);
        assert_eq!(down.to, 2);
        assert_eq!(down.scripts_applied.len(), 2);
        assert!(!store.table_exists("pending_approvals").await.unwrap());
        assert!(!store.table_exists("chunk_tags").await.unwrap());

        // Re-apply v2→v3→v4 and verify both tables come back.
        let up = registry.migrate_up(store.pool()).await.unwrap();
        assert_eq!(up.from, 2);
        assert_eq!(up.to, 4);
        assert_eq!(up.scripts_applied.len(), 2);
        assert!(store.table_exists("pending_approvals").await.unwrap());
        assert!(store.table_exists("chunk_tags").await.unwrap());
    }

    // -- ensure_schema adapter keeps behaving the same ---------------------

    #[tokio::test]
    async fn ensure_schema_adapter_first_boot_initialises() {
        let (store, _tmp) = fresh_store().await;
        wipe_schema_version(&store).await;
        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(out, MigrationOutcome::Initialised(crate::SCHEMA_VERSION));
    }

    #[tokio::test]
    async fn ensure_schema_adapter_up_to_date() {
        let (store, _tmp) = fresh_store().await;
        ensure_schema(&store).await.unwrap();
        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(out, MigrationOutcome::UpToDate(crate::SCHEMA_VERSION));
    }

    #[tokio::test]
    async fn ensure_schema_adapter_runs_v1_to_target() {
        let (store, _tmp) = fresh_store().await;
        let file_id = store.insert_file("d.md", "d", "h", 0, 0).await.unwrap();
        store
            .insert_chunk(file_id, 0, "legacy content needs rebuild", None)
            .await
            .unwrap();
        sqlx::query("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
            .execute(store.pool())
            .await
            .unwrap();
        assert!(store.search_bm25("legacy", 5).await.unwrap().is_empty());
        store.kv_set("schema_version", "1").await.unwrap();

        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(
            out,
            MigrationOutcome::Migrated {
                from: 1,
                to: crate::SCHEMA_VERSION,
            }
        );
        assert_eq!(store.search_bm25("legacy", 5).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn ensure_schema_adapter_rejects_unknown_version() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "99").await.unwrap();
        let err = ensure_schema(&store).await.unwrap_err();
        assert!(
            err.to_string().contains("schema_version mismatch"),
            "unexpected: {err}"
        );
    }
}
