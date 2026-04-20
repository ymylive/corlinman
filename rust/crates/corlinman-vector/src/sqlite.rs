//! sqlx pool + file/chunk/kv access + BM25 FTS5 search.
//!
//! Tables (corlinman-native, schema v2):
//!
//! - `files` — one row per indexed source file.
//! - `chunks` — text chunks + little-endian f32 BLOB vector.
//! - `chunks_fts` — FTS5 contentless-linked virtual table mirroring
//!   `chunks.content`, maintained by INSERT/DELETE/UPDATE triggers.
//! - `kv_store` — general KV cache + `schema_version`.
//!
//! The BM25 path uses SQLite's built-in `bm25()` ranker. FTS5 ships in
//! the sqlx-bundled `libsqlite3-sys` by default (no Cargo feature flip
//! required).

use std::path::Path;
use std::str::FromStr;

use anyhow::{Context, Result};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Row, SqlitePool};

/// Full CREATE TABLE + CREATE INDEX script used when opening a fresh DB.
///
/// All statements are `IF NOT EXISTS`, so this is safe to re-run against
/// an existing DB file. The `chunks_fts` virtual table and its sync
/// triggers are created here so a fresh v2 DB needs no backfill; the
/// v1→v2 backfill path lives in [`crate::migration`].
pub const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    vector BLOB
);

CREATE INDEX IF NOT EXISTS idx_files_diary ON files(diary_name);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
"#;

/// Row from `files`.
#[derive(Debug, Clone, PartialEq)]
pub struct FileRow {
    pub id: i64,
    pub path: String,
    pub diary_name: String,
    pub checksum: String,
    pub mtime: i64,
    pub size: i64,
    pub updated_at: Option<i64>,
}

/// Row from `chunks`.
#[derive(Debug, Clone, PartialEq)]
pub struct ChunkRow {
    pub id: i64,
    pub file_id: i64,
    pub chunk_index: i64,
    pub content: String,
    /// Decoded vector (little-endian f32). `None` if the BLOB is NULL or the
    /// length wasn't a multiple of 4.
    pub vector: Option<Vec<f32>>,
}

/// Thin wrapper over a `SqlitePool` pointed at `knowledge_base.sqlite`.
///
/// Opens the file with WAL + `foreign_keys=ON`; creates tables lazily if
/// the file is brand new.
#[derive(Debug, Clone)]
pub struct SqliteStore {
    pool: SqlitePool,
}

impl SqliteStore {
    /// Open (or create) a SQLite file at `path`.
    ///
    /// Behaviour:
    /// - Creates the file if missing (`create_if_missing(true)`).
    /// - Enables WAL + `synchronous=NORMAL` + `foreign_keys=ON`.
    /// - Runs [`SCHEMA_SQL`] unconditionally (`CREATE … IF NOT EXISTS`).
    pub async fn open(path: &Path) -> Result<Self> {
        let url = format!("sqlite://{}", path.display());
        let options = SqliteConnectOptions::from_str(&url)
            .with_context(|| format!("parse sqlite url '{url}'"))?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .foreign_keys(true);

        let pool = SqlitePoolOptions::new()
            .max_connections(8)
            .connect_with(options)
            .await
            .with_context(|| format!("connect sqlite '{}'", path.display()))?;

        // Run the schema DDL as a single multi-statement script. sqlx's
        // SQLite driver accepts this via `raw_sql`; we can't split on `;`
        // because CREATE TRIGGER bodies contain internal `;` separators.
        sqlx::raw_sql(SCHEMA_SQL)
            .execute(&pool)
            .await
            .context("apply SCHEMA_SQL")?;

        Ok(Self { pool })
    }

    /// Borrow the underlying pool (mostly for tests / migrations).
    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    /// List every row in `files`, ordered by `id ASC`.
    pub async fn list_files(&self) -> Result<Vec<FileRow>> {
        let rows = sqlx::query(
            "SELECT id, path, diary_name, checksum, mtime, size, updated_at \
             FROM files ORDER BY id ASC",
        )
        .fetch_all(&self.pool)
        .await
        .context("list_files")?;

        Ok(rows
            .into_iter()
            .map(|r| FileRow {
                id: r.get::<i64, _>("id"),
                path: r.get::<String, _>("path"),
                diary_name: r.get::<String, _>("diary_name"),
                checksum: r.get::<String, _>("checksum"),
                mtime: r.get::<i64, _>("mtime"),
                size: r.get::<i64, _>("size"),
                updated_at: r.get::<Option<i64>, _>("updated_at"),
            })
            .collect())
    }

    /// Chunks belonging to `file_id`, ordered by `chunk_index`.
    pub async fn get_chunks(&self, file_id: i64) -> Result<Vec<ChunkRow>> {
        let rows = sqlx::query(
            "SELECT id, file_id, chunk_index, content, vector \
             FROM chunks WHERE file_id = ?1 ORDER BY chunk_index ASC",
        )
        .bind(file_id)
        .fetch_all(&self.pool)
        .await
        .with_context(|| format!("get_chunks(file_id={file_id})"))?;

        Ok(rows.into_iter().map(row_to_chunk).collect())
    }

    /// Fetch chunks by a list of ids; preserves caller-supplied order.
    pub async fn query_chunks_by_ids(&self, ids: &[i64]) -> Result<Vec<ChunkRow>> {
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let placeholders = std::iter::repeat_n("?", ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT id, file_id, chunk_index, content, vector \
             FROM chunks WHERE id IN ({placeholders})"
        );
        let mut q = sqlx::query(&sql);
        for id in ids {
            q = q.bind(id);
        }
        let rows = q
            .fetch_all(&self.pool)
            .await
            .context("query_chunks_by_ids")?;
        let mut out: Vec<ChunkRow> = rows.into_iter().map(row_to_chunk).collect();

        // Stable-sort by position in the input slice.
        let order: std::collections::HashMap<i64, usize> =
            ids.iter().enumerate().map(|(i, id)| (*id, i)).collect();
        out.sort_by_key(|c| order.get(&c.id).copied().unwrap_or(usize::MAX));
        Ok(out)
    }

    /// Total row count in `chunks`.
    pub async fn count_chunks(&self) -> Result<i64> {
        let row = sqlx::query("SELECT COUNT(*) AS n FROM chunks")
            .fetch_one(&self.pool)
            .await
            .context("count_chunks")?;
        Ok(row.get::<i64, _>("n"))
    }

    /// BM25 full-text search over `chunks.content` via the `chunks_fts`
    /// FTS5 virtual table.
    ///
    /// Returns `(chunk_id, score)` pairs ordered best-first. FTS5's
    /// `bm25()` returns a non-positive number (smaller = more relevant),
    /// so we negate it here — callers see a positive, larger-is-better
    /// score consistent with the rest of the API.
    ///
    /// `query` is passed to FTS5 as-is; callers that accept untrusted
    /// input should pre-sanitise (or wrap tokens in double quotes) to
    /// neutralise FTS5 query syntax.
    pub async fn search_bm25(&self, query: &str, limit: usize) -> Result<Vec<(i64, f32)>> {
        if query.trim().is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let rows = sqlx::query(
            "SELECT rowid AS id, bm25(chunks_fts) AS score \
             FROM chunks_fts \
             WHERE chunks_fts MATCH ?1 \
             ORDER BY score ASC \
             LIMIT ?2",
        )
        .bind(query)
        .bind(limit as i64)
        .fetch_all(&self.pool)
        .await
        .with_context(|| format!("search_bm25('{query}', limit={limit})"))?;

        Ok(rows
            .into_iter()
            .map(|r| {
                let id = r.get::<i64, _>("id");
                let raw = r.get::<f64, _>("score") as f32;
                (id, -raw)
            })
            .collect())
    }

    /// Backfill `chunks_fts` from the existing `chunks` table.
    ///
    /// Used by the v1→v2 migration: the triggers only fire on future
    /// INSERT/UPDATE/DELETE, so pre-existing rows need one-shot
    /// population via the FTS5 `rebuild` command.
    pub async fn rebuild_fts(&self) -> Result<()> {
        sqlx::query("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            .execute(&self.pool)
            .await
            .context("rebuild chunks_fts")?;
        Ok(())
    }

    // ---- low-level helpers used by tests / migration -----------------------

    /// Insert a row into `files`; returns `lastInsertRowid`.
    pub async fn insert_file(
        &self,
        path: &str,
        diary_name: &str,
        checksum: &str,
        mtime: i64,
        size: i64,
    ) -> Result<i64> {
        let now = time::OffsetDateTime::now_utc().unix_timestamp();
        let res = sqlx::query(
            "INSERT INTO files(path, diary_name, checksum, mtime, size, updated_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        )
        .bind(path)
        .bind(diary_name)
        .bind(checksum)
        .bind(mtime)
        .bind(size)
        .bind(now)
        .execute(&self.pool)
        .await
        .context("insert_file")?;
        Ok(res.last_insert_rowid())
    }

    /// Insert a chunk; returns its auto-assigned `id`.
    pub async fn insert_chunk(
        &self,
        file_id: i64,
        chunk_index: i64,
        content: &str,
        vector: Option<&[f32]>,
    ) -> Result<i64> {
        let blob = vector.map(crate::f32_slice_to_blob);
        let res = sqlx::query(
            "INSERT INTO chunks(file_id, chunk_index, content, vector) \
             VALUES (?1, ?2, ?3, ?4)",
        )
        .bind(file_id)
        .bind(chunk_index)
        .bind(content)
        .bind(blob)
        .execute(&self.pool)
        .await
        .context("insert_chunk")?;
        Ok(res.last_insert_rowid())
    }

    /// Read a `kv_store` string value by key.
    pub async fn kv_get(&self, key: &str) -> Result<Option<String>> {
        let row = sqlx::query("SELECT value FROM kv_store WHERE key = ?1")
            .bind(key)
            .fetch_optional(&self.pool)
            .await
            .with_context(|| format!("kv_get({key})"))?;
        Ok(row.and_then(|r| r.get::<Option<String>, _>("value")))
    }

    /// Upsert a `kv_store` string value.
    pub async fn kv_set(&self, key: &str, value: &str) -> Result<()> {
        sqlx::query("INSERT OR REPLACE INTO kv_store(key, value, vector) VALUES (?1, ?2, NULL)")
            .bind(key)
            .bind(value)
            .execute(&self.pool)
            .await
            .with_context(|| format!("kv_set({key})"))?;
        Ok(())
    }

    /// Check whether a named table exists (used by migrations).
    pub async fn table_exists(&self, name: &str) -> Result<bool> {
        let row =
            sqlx::query("SELECT name FROM sqlite_master WHERE type='table' AND name = ?1 LIMIT 1")
                .bind(name)
                .fetch_optional(&self.pool)
                .await
                .with_context(|| format!("table_exists({name})"))?;
        Ok(row.is_some())
    }
}

fn row_to_chunk(r: sqlx::sqlite::SqliteRow) -> ChunkRow {
    ChunkRow {
        id: r.get::<i64, _>("id"),
        file_id: r.get::<i64, _>("file_id"),
        chunk_index: r.get::<i64, _>("chunk_index"),
        content: r.get::<String, _>("content"),
        vector: r
            .get::<Option<Vec<u8>>, _>("vector")
            .and_then(|b| crate::blob_to_f32_vec(&b)),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    async fn fresh_store() -> (SqliteStore, TempDir) {
        let tmp = TempDir::new().expect("tempdir");
        let path = tmp.path().join("kb.sqlite");
        let store = SqliteStore::open(&path).await.expect("open");
        (store, tmp)
    }

    #[tokio::test]
    async fn open_creates_schema() {
        let (store, _tmp) = fresh_store().await;
        for t in ["files", "chunks", "kv_store", "chunks_fts"] {
            assert!(store.table_exists(t).await.unwrap(), "table {t} missing");
        }
    }

    #[tokio::test]
    async fn bm25_search_returns_matching_rows() {
        let (store, _tmp) = fresh_store().await;
        let file_id = store
            .insert_file("doc.md", "default", "h", 0, 0)
            .await
            .unwrap();
        let _ = store
            .insert_chunk(file_id, 0, "the quick brown fox jumps", None)
            .await
            .unwrap();
        let target = store
            .insert_chunk(file_id, 1, "lazy dog sleeps in the sun", None)
            .await
            .unwrap();
        let _ = store
            .insert_chunk(file_id, 2, "unrelated content about cats", None)
            .await
            .unwrap();

        let hits = store.search_bm25("lazy dog", 5).await.unwrap();
        assert!(!hits.is_empty(), "BM25 should return matches");
        assert_eq!(hits[0].0, target, "'lazy dog' row must rank first");
        assert!(hits[0].1 > 0.0, "score must be positive, got {}", hits[0].1);
    }

    #[tokio::test]
    async fn bm25_empty_query_returns_empty() {
        let (store, _tmp) = fresh_store().await;
        assert!(store.search_bm25("   ", 5).await.unwrap().is_empty());
        assert!(store.search_bm25("anything", 0).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn fts_trigger_keeps_index_in_sync_on_delete() {
        let (store, _tmp) = fresh_store().await;
        let file_id = store
            .insert_file("d.md", "default", "h", 0, 0)
            .await
            .unwrap();
        let _ = store
            .insert_chunk(file_id, 0, "alpha bravo charlie", None)
            .await
            .unwrap();
        assert_eq!(store.search_bm25("alpha", 5).await.unwrap().len(), 1);

        sqlx::query("DELETE FROM files WHERE id = ?1")
            .bind(file_id)
            .execute(store.pool())
            .await
            .unwrap();
        assert!(store.search_bm25("alpha", 5).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn empty_lists_are_empty() {
        let (store, _tmp) = fresh_store().await;
        assert_eq!(store.list_files().await.unwrap(), vec![]);
        assert_eq!(store.count_chunks().await.unwrap(), 0);
    }

    #[tokio::test]
    async fn insert_and_query_chunks() {
        let (store, _tmp) = fresh_store().await;
        let file_id = store
            .insert_file(
                "公共/2026-04-20.md",
                "公共",
                "deadbeef",
                1_700_000_000,
                1024,
            )
            .await
            .unwrap();
        let v1 = vec![0.1_f32, 0.2, 0.3];
        let v2 = vec![0.4_f32, 0.5, 0.6];
        let c1 = store
            .insert_chunk(file_id, 0, "hello world", Some(&v1))
            .await
            .unwrap();
        let c2 = store
            .insert_chunk(file_id, 1, "second chunk", Some(&v2))
            .await
            .unwrap();

        let got = store.get_chunks(file_id).await.unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].id, c1);
        assert_eq!(got[0].content, "hello world");
        assert_eq!(got[0].vector.as_deref(), Some(v1.as_slice()));
        assert_eq!(got[1].id, c2);

        let got = store.query_chunks_by_ids(&[c2, c1]).await.unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].id, c2);
        assert_eq!(got[1].id, c1);

        assert_eq!(store.count_chunks().await.unwrap(), 2);
    }

    #[tokio::test]
    async fn reopen_is_idempotent() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("kb.sqlite");

        {
            let store = SqliteStore::open(&path).await.unwrap();
            store.insert_file("a.md", "d", "h", 0, 0).await.unwrap();
            store.kv_set("schema_version", "2").await.unwrap();
        }
        let store = SqliteStore::open(&path).await.unwrap();
        assert_eq!(store.list_files().await.unwrap().len(), 1);
        assert_eq!(
            store.kv_get("schema_version").await.unwrap().as_deref(),
            Some("2")
        );
    }

    #[tokio::test]
    async fn query_chunks_by_ids_empty_input_is_empty_output() {
        let (store, _tmp) = fresh_store().await;
        assert_eq!(store.query_chunks_by_ids(&[]).await.unwrap(), vec![]);
    }

    #[tokio::test]
    async fn rebuild_fts_populates_rows_inserted_outside_triggers() {
        let (store, _tmp) = fresh_store().await;
        let file_id = store
            .insert_file("d.md", "default", "h", 0, 0)
            .await
            .unwrap();
        let _ = store
            .insert_chunk(file_id, 0, "hello rebuild world", None)
            .await
            .unwrap();

        // Simulate a v1→v2 scenario: nuke FTS contents and rebuild.
        sqlx::query("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
            .execute(store.pool())
            .await
            .unwrap();
        assert!(store.search_bm25("rebuild", 5).await.unwrap().is_empty());

        store.rebuild_fts().await.unwrap();
        assert_eq!(store.search_bm25("rebuild", 5).await.unwrap().len(), 1);
    }

    #[test]
    fn schema_sql_not_empty() {
        assert!(SCHEMA_SQL.contains("CREATE TABLE"));
        assert!(SCHEMA_SQL.contains("chunks_fts"));
    }
}
