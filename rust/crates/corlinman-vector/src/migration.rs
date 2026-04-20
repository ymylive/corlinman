//! Schema migrations for the corlinman SQLite store.
//!
//! The only migration corlinman-native carries today is **v1 → v2**: a
//! DB written before FTS5 was part of the baseline needs the
//! `chunks_fts` virtual table populated from existing `chunks` rows
//! (the triggers handle everything from that point forward).
//!
//! # Behaviour
//!
//! On open, [`ensure_schema`] reads `kv_store.schema_version`:
//!
//! - **Missing** — fresh DB. Write [`crate::SCHEMA_VERSION`] and return
//!   [`MigrationOutcome::Initialised`].
//! - **Equals `SCHEMA_VERSION`** — no-op, returns
//!   [`MigrationOutcome::UpToDate`].
//! - **`1`** — pre-FTS5 layout. Run [`SqliteStore::rebuild_fts`],
//!   bump the stored version, return [`MigrationOutcome::Migrated`].
//! - **Anything else** — error. We refuse to auto-downgrade or
//!   guess-forward from unknown versions.
//!
//! # TODO
//!
//! - A `MigrationScript { from: u32, to: u32, run: async fn(...) }`
//!   registry once we have more than one step.
//! - `.usearch` header compat probe (convert-on-mismatch).

use anyhow::{anyhow, Result};

use crate::sqlite::SqliteStore;

/// Outcome of [`ensure_schema`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MigrationOutcome {
    /// `kv_store` had no `schema_version`; we wrote the current one.
    Initialised(i64),
    /// `schema_version` matched [`crate::SCHEMA_VERSION`] — nothing to do.
    UpToDate(i64),
    /// We ran a migration script. Payload: `(from, to)`.
    Migrated { from: i64, to: i64 },
}

/// Bootstrap / verify `schema_version` in `kv_store`, running any
/// required forward migrations.
pub async fn ensure_schema(store: &SqliteStore) -> Result<MigrationOutcome> {
    match store.kv_get("schema_version").await? {
        None => {
            store
                .kv_set("schema_version", &crate::SCHEMA_VERSION.to_string())
                .await?;
            Ok(MigrationOutcome::Initialised(crate::SCHEMA_VERSION))
        }
        Some(v) => {
            let parsed: i64 = v
                .parse()
                .map_err(|e| anyhow!("kv_store.schema_version='{v}' not an integer: {e}"))?;
            if parsed == crate::SCHEMA_VERSION {
                Ok(MigrationOutcome::UpToDate(parsed))
            } else if parsed == 1 && crate::SCHEMA_VERSION == 2 {
                // v1 → v2: the FTS5 virtual table + triggers already exist
                // (SCHEMA_SQL in SqliteStore::open is idempotent). All we
                // need is a one-shot backfill so pre-existing chunk rows
                // become searchable.
                store
                    .rebuild_fts()
                    .await
                    .map_err(|e| anyhow!("v1→v2 FTS5 backfill failed: {e}"))?;
                store
                    .kv_set("schema_version", &crate::SCHEMA_VERSION.to_string())
                    .await?;
                Ok(MigrationOutcome::Migrated {
                    from: 1,
                    to: crate::SCHEMA_VERSION,
                })
            } else {
                Err(anyhow!(
                    "schema_version mismatch: stored={} current={}; no migration path",
                    parsed,
                    crate::SCHEMA_VERSION
                ))
            }
        }
    }
}

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

    #[tokio::test]
    async fn first_boot_writes_schema_version() {
        let (store, _tmp) = fresh_store().await;
        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(out, MigrationOutcome::Initialised(crate::SCHEMA_VERSION));
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("2")
        );
    }

    #[tokio::test]
    async fn second_boot_is_no_op() {
        let (store, _tmp) = fresh_store().await;
        ensure_schema(&store).await.unwrap();
        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(out, MigrationOutcome::UpToDate(crate::SCHEMA_VERSION));
    }

    #[tokio::test]
    async fn v1_to_v2_backfills_fts_and_bumps_version() {
        let (store, _tmp) = fresh_store().await;

        // Simulate a legacy v1 DB: chunks row inserted, then FTS5 purged,
        // version pinned to 1.
        let file_id = store.insert_file("d.md", "d", "h", 0, 0).await.unwrap();
        store
            .insert_chunk(file_id, 0, "legacy content needs rebuild", None)
            .await
            .unwrap();
        // Wipe FTS5 so we can prove the migration is what repopulates it.
        sqlx::query("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
            .execute(store.pool())
            .await
            .unwrap();
        assert!(store.search_bm25("legacy", 5).await.unwrap().is_empty());
        store.kv_set("schema_version", "1").await.unwrap();

        let out = ensure_schema(&store).await.unwrap();
        assert_eq!(out, MigrationOutcome::Migrated { from: 1, to: 2 });
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("2")
        );
        // BM25 now returns the backfilled row.
        assert_eq!(store.search_bm25("legacy", 5).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn unknown_version_errors() {
        let (store, _tmp) = fresh_store().await;
        store.kv_set("schema_version", "99").await.unwrap();
        let err = ensure_schema(&store).await.unwrap_err().to_string();
        assert!(err.contains("schema_version mismatch"), "{err}");
    }
}
