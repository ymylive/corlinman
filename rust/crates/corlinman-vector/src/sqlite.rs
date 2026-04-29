//! sqlx pool + file/chunk/kv access + BM25 FTS5 search.
//!
//! Tables (corlinman-native, schema v6):
//!
//! - `files` — one row per indexed source file.
//! - `chunks` — text chunks + little-endian f32 BLOB vector + namespace tag
//!   (Sprint 9 T1, `default 'general'`). Namespace partitions the corpus for
//!   the diary / paper-reader / general RAG split.
//! - `chunks_fts` — FTS5 contentless-linked virtual table mirroring
//!   `chunks.content`, maintained by INSERT/DELETE/UPDATE triggers.
//! - `tag_nodes` — hierarchical tag tree (Sprint 9 T-B3-BE3). Each row is
//!   a node in a dotted-path tree (`role.protagonist.voice`); `parent_id`
//!   threads the hierarchy, `path` is the dotted full path (UNIQUE), and
//!   `depth` is 0 for roots.
//! - `chunk_tags` — (chunk_id, tag_node_id) many-to-many retargeted in v6
//!   to reference `tag_nodes.id` instead of a flat TEXT tag.
//! - `chunk_epa` — per-chunk EPA cache (Sprint 9 T-B3-BE3). One row per
//!   processed chunk: packed f32[] projections BLOB, entropy + logic_depth
//!   REAL scalars. Populated lazily by the TagMemo engine (B3-BE4).
//! - `kv_store` — general KV cache + `schema_version`.
//! - `pending_approvals` — one row per tool call that hit a `prompt`
//!   approval rule; consumed by the `/admin/approvals` UI.
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
    updated_at INTEGER,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    namespace TEXT NOT NULL DEFAULT 'general',
    decay_score REAL NOT NULL DEFAULT 1.0,
    consolidated_at INTEGER,
    last_recalled_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    tenant_id TEXT NOT NULL DEFAULT 'default',
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    vector BLOB,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_files_diary ON files(diary_name);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_namespace ON chunks(namespace, id);

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

CREATE TABLE IF NOT EXISTS pending_approvals (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    plugin TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decision TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_requested
    ON pending_approvals(requested_at);

CREATE TABLE IF NOT EXISTS tag_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES tag_nodes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    depth       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    tenant_id   TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_tag_nodes_parent ON tag_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_tag_nodes_path   ON tag_nodes(path);
CREATE INDEX IF NOT EXISTS idx_tag_nodes_depth  ON tag_nodes(depth);

CREATE TABLE IF NOT EXISTS chunk_tags (
    chunk_id     INTEGER NOT NULL,
    tag_node_id  INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, tag_node_id),
    FOREIGN KEY (chunk_id)    REFERENCES chunks(id)    ON DELETE CASCADE,
    FOREIGN KEY (tag_node_id) REFERENCES tag_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunk_tags_tag_node ON chunk_tags(tag_node_id);

CREATE TABLE IF NOT EXISTS chunk_epa (
    chunk_id     INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    projections  BLOB    NOT NULL,
    entropy      REAL    NOT NULL,
    logic_depth  REAL    NOT NULL,
    computed_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"#;

/// Indexes that reference the Phase 4 W1 4-1A `tenant_id` column. Run
/// *after* `ensure_tenant_columns` so the column exists on legacy v6
/// DBs before SQLite tries to build an index on it.
///
/// Includes the DROP+CREATE swap for `idx_pending_approvals_undecided`:
/// SQLite cannot ALTER an index, so the only path to a tenant-aware
/// partial index is to drop the old one and recreate. Both statements
/// are idempotent against an already-migrated DB (DROP IF EXISTS
/// no-ops when the old index is already gone; CREATE IF NOT EXISTS
/// no-ops when the new one already exists).
const TENANT_INDEXES_SQL: &str = r#"
CREATE INDEX IF NOT EXISTS idx_files_tenant_diary
    ON files(tenant_id, diary_name);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_namespace
    ON chunks(tenant_id, namespace, id);
CREATE INDEX IF NOT EXISTS idx_kv_tenant
    ON kv_store(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tag_nodes_tenant_path
    ON tag_nodes(tenant_id, path);

DROP INDEX IF EXISTS idx_pending_approvals_undecided;

CREATE INDEX IF NOT EXISTS idx_pending_approvals_tenant_undecided
    ON pending_approvals(tenant_id, decided_at)
    WHERE decided_at IS NULL;
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
    /// Sprint 9 T1: namespace partition this chunk belongs to. Legacy rows
    /// default to `"general"` per the v4→v5 migration; callers that don't
    /// care pass `"general"` to [`SqliteStore::insert_chunk`].
    pub namespace: String,
}

/// Row from `tag_nodes` (schema v6, hierarchical tag tree).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TagNodeRow {
    pub id: i64,
    pub parent_id: Option<i64>,
    pub name: String,
    pub path: String,
    pub depth: i32,
}

/// Row from `chunk_epa` — per-chunk EPA cache (projections / entropy /
/// logic_depth). Populated lazily by the TagMemo engine; a missing row
/// means the chunk has not been processed yet.
#[derive(Debug, Clone, PartialEq)]
pub struct ChunkEpaRow {
    pub chunk_id: i64,
    /// Packed little-endian f32[] projections.
    pub projections: Vec<f32>,
    pub entropy: f32,
    pub logic_depth: f32,
    pub computed_at: i64,
}

/// Row from `pending_approvals` — one per tool call intercepted by an
/// approval rule set to `mode = "prompt"`.
///
/// `requested_at` and `decided_at` are ISO 8601 strings (RFC 3339 profile)
/// produced by `time::OffsetDateTime::format(&Rfc3339)`. `decision` is
/// `None` while the row is awaiting an operator; it becomes
/// `"approved" | "denied" | "timeout"` once the gate resolves the call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingApproval {
    pub id: String,
    pub session_key: String,
    pub plugin: String,
    pub tool: String,
    pub args_json: String,
    pub requested_at: String,
    pub decided_at: Option<String>,
    pub decision: Option<String>,
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

        // Phase 3 W3-A: idempotent decay column additions for legacy v6 DBs.
        // SCHEMA_SQL above declares the columns on fresh DBs, but pre-W3-A
        // v6 files have the chunks table without them. ALTER TABLE ... ADD
        // COLUMN is the only DDL we need (no FTS rewrite, no index drop)
        // and probing `pragma_table_info` keeps the call idempotent so a
        // re-open of an already-migrated file is a no-op.
        ensure_decay_columns(&pool).await?;

        // Phase 4 W1 4-1A: idempotent tenant_id column additions for the
        // five top-level tables. Same pragma-probe + ALTER pattern as the
        // decay columns above; pre-Phase-4 DBs converge by adding the
        // column with `DEFAULT 'default'` so legacy rows backfill at
        // ALTER time without a separate UPDATE.
        ensure_tenant_columns(&pool).await?;

        // Indexes that reference `tenant_id` must be created *after* the
        // ensure_tenant_columns call so legacy DBs have the column in
        // place before SQLite resolves index column names.
        sqlx::raw_sql(TENANT_INDEXES_SQL)
            .execute(&pool)
            .await
            .context("apply TENANT_INDEXES_SQL")?;

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
            "SELECT id, file_id, chunk_index, content, vector, namespace \
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
            "SELECT id, file_id, chunk_index, content, vector, namespace \
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

    /// BM25 search restricted to a caller-supplied `allowed_ids` whitelist
    /// (used by [`crate::hybrid::HybridSearcher`] for tag-filter pushdown).
    ///
    /// `None` ⇒ behaves identically to [`Self::search_bm25`]. `Some(&[])`
    /// ⇒ returns no hits without ever hitting SQLite.
    pub async fn search_bm25_with_filter(
        &self,
        query: &str,
        limit: usize,
        allowed_ids: Option<&[i64]>,
    ) -> Result<Vec<(i64, f32)>> {
        if query.trim().is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        match allowed_ids {
            None => self.search_bm25(query, limit).await,
            Some([]) => Ok(Vec::new()),
            Some(ids) => {
                let placeholders = std::iter::repeat_n("?", ids.len())
                    .collect::<Vec<_>>()
                    .join(",");
                // All-unnumbered `?` so sqlx binds by textual order —
                // mixing `?N` with `?` confuses sqlx's positional binder.
                let sql = format!(
                    "SELECT rowid AS id, bm25(chunks_fts) AS score \
                     FROM chunks_fts \
                     WHERE chunks_fts MATCH ? AND rowid IN ({placeholders}) \
                     ORDER BY score ASC \
                     LIMIT ?"
                );
                let mut q = sqlx::query(&sql).bind(query);
                for id in ids {
                    q = q.bind(id);
                }
                q = q.bind(limit as i64);
                let rows = q.fetch_all(&self.pool).await.with_context(|| {
                    format!("search_bm25_with_filter('{query}', limit={limit})")
                })?;
                Ok(rows
                    .into_iter()
                    .map(|r| {
                        let id = r.get::<i64, _>("id");
                        let raw = r.get::<f64, _>("score") as f32;
                        (id, -raw)
                    })
                    .collect())
            }
        }
    }

    // ---- chunk_tags (schema v6, hierarchical) ----------------------------

    /// Attach `tag` (a dotted path, possibly single-segment) to `chunk_id`.
    ///
    /// Idempotent on both the `tag_nodes` side (intermediate segments are
    /// upserted via [`Self::ensure_tag_path`]) and the `chunk_tags` side
    /// (`INSERT OR IGNORE`). Pre-v6 callers passing a flat string like
    /// `"rust"` see the same semantics as before: the node is created at
    /// depth 0 and attached.
    pub async fn insert_tag(&self, chunk_id: i64, tag: &str) -> Result<()> {
        let node_id = self.ensure_tag_path(tag).await?;
        sqlx::query("INSERT OR IGNORE INTO chunk_tags(chunk_id, tag_node_id) VALUES (?1, ?2)")
            .bind(chunk_id)
            .bind(node_id)
            .execute(&self.pool)
            .await
            .with_context(|| format!("insert_tag(chunk_id={chunk_id}, tag={tag})"))?;
        Ok(())
    }

    /// Tags attached to `chunk_id` as dotted paths, sorted ascending.
    pub async fn get_tags(&self, chunk_id: i64) -> Result<Vec<String>> {
        let rows = sqlx::query(
            "SELECT tn.path AS path FROM chunk_tags ct \
             JOIN tag_nodes tn ON tn.id = ct.tag_node_id \
             WHERE ct.chunk_id = ?1 ORDER BY tn.path ASC",
        )
        .bind(chunk_id)
        .fetch_all(&self.pool)
        .await
        .with_context(|| format!("get_tags({chunk_id})"))?;
        Ok(rows
            .into_iter()
            .map(|r| r.get::<String, _>("path"))
            .collect())
    }

    /// Upsert every segment of a dotted `path` into `tag_nodes` and return
    /// the leaf node's id. `"role.protagonist.voice"` produces (or reuses)
    /// three rows: `role`, `role.protagonist`, `role.protagonist.voice`.
    ///
    /// Empty or whitespace-only `path` segments yield a `Storage` error —
    /// callers are expected to pass already-normalised paths.
    pub async fn ensure_tag_path(&self, path: &str) -> Result<i64> {
        if path.is_empty() || path.split('.').any(|seg| seg.trim().is_empty()) {
            return Err(anyhow::anyhow!("ensure_tag_path: invalid path '{path}'"));
        }
        let segments: Vec<&str> = path.split('.').collect();
        let mut parent_id: Option<i64> = None;
        let mut cur_path = String::new();
        let mut last_id: i64 = 0;
        for (depth, seg) in segments.iter().enumerate() {
            if depth == 0 {
                cur_path.push_str(seg);
            } else {
                cur_path.push('.');
                cur_path.push_str(seg);
            }
            // Try SELECT first for idempotency; fall back to INSERT if missing.
            let existing: Option<i64> =
                sqlx::query_scalar("SELECT id FROM tag_nodes WHERE path = ?1")
                    .bind(&cur_path)
                    .fetch_optional(&self.pool)
                    .await
                    .with_context(|| format!("ensure_tag_path: lookup '{cur_path}'"))?;
            let id = match existing {
                Some(id) => id,
                None => {
                    let res = sqlx::query(
                        "INSERT INTO tag_nodes(parent_id, name, path, depth) \
                         VALUES (?1, ?2, ?3, ?4)",
                    )
                    .bind(parent_id)
                    .bind(*seg)
                    .bind(&cur_path)
                    .bind(depth as i64)
                    .execute(&self.pool)
                    .await
                    .with_context(|| format!("ensure_tag_path: insert '{cur_path}'"))?;
                    res.last_insert_rowid()
                }
            };
            parent_id = Some(id);
            last_id = id;
        }
        Ok(last_id)
    }

    /// Attach `chunk_id` to the leaf node addressed by `path`, upserting
    /// intermediate nodes as needed. Wrapper around
    /// [`Self::ensure_tag_path`] + an `INSERT OR IGNORE` on `chunk_tags`.
    pub async fn attach_chunk_to_tag_path(&self, chunk_id: i64, path: &str) -> Result<()> {
        let node_id = self.ensure_tag_path(path).await?;
        sqlx::query("INSERT OR IGNORE INTO chunk_tags(chunk_id, tag_node_id) VALUES (?1, ?2)")
            .bind(chunk_id)
            .bind(node_id)
            .execute(&self.pool)
            .await
            .with_context(|| {
                format!("attach_chunk_to_tag_path(chunk_id={chunk_id}, path={path})")
            })?;
        Ok(())
    }

    /// Expand a tag-tree root `path` into the set of `chunk.id`s tagged
    /// anywhere in its subtree (the root itself plus every descendant).
    ///
    /// Matches `tag_nodes.path = ?` OR `tag_nodes.path LIKE ? || '.%'`.
    /// Returned ids are sorted ascending.
    pub async fn filter_chunk_ids_by_tag_subtree(&self, path: &str) -> Result<Vec<i64>> {
        let like_pattern = format!("{path}.%");
        let rows = sqlx::query(
            "SELECT DISTINCT ct.chunk_id AS chunk_id FROM chunk_tags ct \
             JOIN tag_nodes tn ON tn.id = ct.tag_node_id \
             WHERE tn.path = ?1 OR tn.path LIKE ?2 \
             ORDER BY ct.chunk_id ASC",
        )
        .bind(path)
        .bind(&like_pattern)
        .fetch_all(&self.pool)
        .await
        .with_context(|| format!("filter_chunk_ids_by_tag_subtree({path})"))?;
        Ok(rows
            .into_iter()
            .map(|r| r.get::<i64, _>("chunk_id"))
            .collect())
    }

    /// Resolve a [`crate::hybrid::TagFilter`] into the sorted set of
    /// `chunk.id`s that satisfy it (required ∧ any_of ∧ ¬excluded).
    ///
    /// Semantics:
    /// - `required`: chunk must carry *every* tag listed (matched against
    ///   `tag_nodes.path` — a flat tag is simply a depth-0 path).
    /// - `any_of`: chunk must carry *at least one* tag listed (ignored when empty).
    /// - `excluded`: chunk must carry *none* of the tags listed.
    /// - All empty ⇒ returns every `chunks.id`.
    ///
    /// Subtree queries live on [`Self::filter_chunk_ids_by_tag_subtree`]
    /// and are combined by [`crate::hybrid::HybridSearcher::search`] so
    /// [`crate::hybrid::TagFilter`] stays field-stable for downstream crates.
    pub async fn filter_chunk_ids_by_tags(
        &self,
        filter: &crate::hybrid::TagFilter,
    ) -> Result<Vec<i64>> {
        let req = &filter.required;
        let any = &filter.any_of;
        let exc = &filter.excluded;

        // Empty filter ⇒ every chunk id; callers treat "all chunks" as
        // "no filter applied".
        if req.is_empty() && any.is_empty() && exc.is_empty() {
            let rows = sqlx::query("SELECT id FROM chunks ORDER BY id ASC")
                .fetch_all(&self.pool)
                .await
                .context("filter_chunk_ids_by_tags: list all")?;
            return Ok(rows.into_iter().map(|r| r.get::<i64, _>("id")).collect());
        }

        // Build the SQL incrementally. `HAVING COUNT(DISTINCT ..) = N`
        // implements the conjunction for `required`. All tag predicates
        // match against `tag_nodes.path` so a flat v5-style tag like
        // `"rust"` becomes a depth-0 path `"rust"` — the identical
        // semantics callers had in v5 still hold.
        let mut sql = String::from("SELECT DISTINCT c.id FROM chunks c");
        let mut binds: Vec<String> = Vec::new();
        let mut where_clauses: Vec<String> = Vec::new();

        if !req.is_empty() {
            sql.push_str(
                " JOIN chunk_tags ct_req ON ct_req.chunk_id = c.id \
                  JOIN tag_nodes tn_req ON tn_req.id = ct_req.tag_node_id",
            );
            let placeholders = std::iter::repeat_n("?", req.len())
                .collect::<Vec<_>>()
                .join(",");
            where_clauses.push(format!("tn_req.path IN ({placeholders})"));
            for t in req {
                binds.push(t.clone());
            }
        }

        if !any.is_empty() {
            let placeholders = std::iter::repeat_n("?", any.len())
                .collect::<Vec<_>>()
                .join(",");
            where_clauses.push(format!(
                "EXISTS (SELECT 1 FROM chunk_tags ct_any \
                 JOIN tag_nodes tn_any ON tn_any.id = ct_any.tag_node_id \
                 WHERE ct_any.chunk_id = c.id AND tn_any.path IN ({placeholders}))"
            ));
            for t in any {
                binds.push(t.clone());
            }
        }

        if !exc.is_empty() {
            let placeholders = std::iter::repeat_n("?", exc.len())
                .collect::<Vec<_>>()
                .join(",");
            where_clauses.push(format!(
                "NOT EXISTS (SELECT 1 FROM chunk_tags ct_exc \
                 JOIN tag_nodes tn_exc ON tn_exc.id = ct_exc.tag_node_id \
                 WHERE ct_exc.chunk_id = c.id AND tn_exc.path IN ({placeholders}))"
            ));
            for t in exc {
                binds.push(t.clone());
            }
        }

        if !where_clauses.is_empty() {
            sql.push_str(" WHERE ");
            sql.push_str(&where_clauses.join(" AND "));
        }

        if !req.is_empty() {
            sql.push_str(&format!(
                " GROUP BY c.id HAVING COUNT(DISTINCT tn_req.path) = {}",
                req.len()
            ));
        }

        sql.push_str(" ORDER BY c.id ASC");

        let mut q = sqlx::query(&sql);
        for b in &binds {
            q = q.bind(b);
        }
        let rows = q
            .fetch_all(&self.pool)
            .await
            .context("filter_chunk_ids_by_tags")?;
        Ok(rows.into_iter().map(|r| r.get::<i64, _>("id")).collect())
    }

    // ---- chunk_epa (schema v6) -------------------------------------------

    /// Upsert a per-chunk EPA cache row. `projections` is encoded as a
    /// little-endian f32 BLOB; `entropy` and `logic_depth` are stored as
    /// REALs. Running twice on the same `chunk_id` replaces the prior
    /// row and refreshes `computed_at` to now.
    pub async fn upsert_chunk_epa(
        &self,
        chunk_id: i64,
        projections: &[f32],
        entropy: f32,
        logic_depth: f32,
    ) -> Result<()> {
        let blob = crate::f32_slice_to_blob(projections);
        let now = time::OffsetDateTime::now_utc().unix_timestamp();
        sqlx::query(
            "INSERT INTO chunk_epa(chunk_id, projections, entropy, logic_depth, computed_at) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(chunk_id) DO UPDATE SET \
                projections = excluded.projections, \
                entropy     = excluded.entropy, \
                logic_depth = excluded.logic_depth, \
                computed_at = excluded.computed_at",
        )
        .bind(chunk_id)
        .bind(blob)
        .bind(entropy as f64)
        .bind(logic_depth as f64)
        .bind(now)
        .execute(&self.pool)
        .await
        .with_context(|| format!("upsert_chunk_epa(chunk_id={chunk_id})"))?;
        Ok(())
    }

    /// Fetch the per-chunk EPA cache row, if the TagMemo engine has
    /// populated it. Returns `None` for chunks that have not been
    /// processed yet.
    pub async fn get_chunk_epa(&self, chunk_id: i64) -> Result<Option<ChunkEpaRow>> {
        let row = sqlx::query(
            "SELECT chunk_id, projections, entropy, logic_depth, computed_at \
             FROM chunk_epa WHERE chunk_id = ?1",
        )
        .bind(chunk_id)
        .fetch_optional(&self.pool)
        .await
        .with_context(|| format!("get_chunk_epa({chunk_id})"))?;
        Ok(row.map(|r| {
            let blob: Vec<u8> = r.get("projections");
            ChunkEpaRow {
                chunk_id: r.get::<i64, _>("chunk_id"),
                projections: crate::blob_to_f32_vec(&blob).unwrap_or_default(),
                entropy: r.get::<f64, _>("entropy") as f32,
                logic_depth: r.get::<f64, _>("logic_depth") as f32,
                computed_at: r.get::<i64, _>("computed_at"),
            }
        }))
    }

    // ---- namespace helpers (schema v5) -----------------------------------

    /// List every distinct `chunks.namespace` value along with the chunk
    /// count in that namespace. Sorted ascending by name.
    ///
    /// Sprint 9 T1: powers `corlinman vector namespaces` + the admin UI's
    /// future memory-dashboard namespace picker.
    pub async fn list_namespaces(&self) -> Result<Vec<(String, u64)>> {
        let rows = sqlx::query(
            "SELECT namespace, COUNT(*) AS n \
             FROM chunks GROUP BY namespace ORDER BY namespace ASC",
        )
        .fetch_all(&self.pool)
        .await
        .context("list_namespaces")?;
        Ok(rows
            .into_iter()
            .map(|r| {
                let name: String = r.get("namespace");
                let n: i64 = r.get("n");
                (name, n.max(0) as u64)
            })
            .collect())
    }

    /// Intersect a (possibly-`None`) tag-filtered id whitelist with the
    /// set of chunk ids that live in one of `namespaces`. When both are
    /// `None` this returns `None` (meaning "no filter"); otherwise the
    /// returned `Vec<i64>` is the sorted intersection.
    ///
    /// An empty `namespaces` slice is treated as "no namespace filter" —
    /// callers who want to restrict to zero namespaces should short-circuit
    /// before calling this.
    pub async fn filter_chunk_ids_by_namespace(&self, namespaces: &[String]) -> Result<Vec<i64>> {
        if namespaces.is_empty() {
            // No filter ⇒ all ids. Caller combines with any tag-filter result.
            let rows = sqlx::query("SELECT id FROM chunks ORDER BY id ASC")
                .fetch_all(&self.pool)
                .await
                .context("filter_chunk_ids_by_namespace: list all")?;
            return Ok(rows.into_iter().map(|r| r.get::<i64, _>("id")).collect());
        }
        let placeholders = std::iter::repeat_n("?", namespaces.len())
            .collect::<Vec<_>>()
            .join(",");
        let sql =
            format!("SELECT id FROM chunks WHERE namespace IN ({placeholders}) ORDER BY id ASC");
        let mut q = sqlx::query(&sql);
        for ns in namespaces {
            q = q.bind(ns);
        }
        let rows = q
            .fetch_all(&self.pool)
            .await
            .context("filter_chunk_ids_by_namespace")?;
        Ok(rows.into_iter().map(|r| r.get::<i64, _>("id")).collect())
    }

    /// Total row count in `files`.
    pub async fn count_files(&self) -> Result<i64> {
        let row = sqlx::query("SELECT COUNT(*) AS n FROM files")
            .fetch_one(&self.pool)
            .await
            .context("count_files")?;
        Ok(row.get::<i64, _>("n"))
    }

    /// Distinct tag count across `chunk_tags` (distinct `tag_nodes.path`
    /// values reachable through an attachment). Schema v6: a flat v5 tag
    /// `"rust"` is a depth-0 node `path = "rust"`, so counts match the
    /// pre-migration semantics for flat-tag callers.
    pub async fn count_tags(&self) -> Result<i64> {
        let row = sqlx::query(
            "SELECT COUNT(DISTINCT tn.path) AS n FROM chunk_tags ct \
             JOIN tag_nodes tn ON tn.id = ct.tag_node_id",
        )
        .fetch_one(&self.pool)
        .await
        .context("count_tags")?;
        Ok(row.get::<i64, _>("n"))
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
    ///
    /// Sprint 9 T1: `namespace` partitions the corpus for the diary /
    /// paper-reader / general RAG split. Legacy callers pass `"general"`
    /// (matching the column default) to preserve pre-S9 behaviour.
    pub async fn insert_chunk(
        &self,
        file_id: i64,
        chunk_index: i64,
        content: &str,
        vector: Option<&[f32]>,
        namespace: &str,
    ) -> Result<i64> {
        let blob = vector.map(crate::f32_slice_to_blob);
        let res = sqlx::query(
            "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
        )
        .bind(file_id)
        .bind(chunk_index)
        .bind(content)
        .bind(blob)
        .bind(namespace)
        .execute(&self.pool)
        .await
        .context("insert_chunk")?;
        Ok(res.last_insert_rowid())
    }

    /// Delete a single chunk by id.
    ///
    /// The `chunks_ad` trigger keeps `chunks_fts` in sync. Returns
    /// the number of rows removed (0 when `id` is unknown). Added in
    /// the memory-host skeleton (Phase 1) so `LocalSqliteHost::delete`
    /// has a non-SQL-duplicating call path; callers outside the
    /// memory-host crate can use this for surgical chunk removal
    /// without touching the owning `files` row.
    pub async fn delete_chunk_by_id(&self, id: i64) -> Result<u64> {
        let res = sqlx::query("DELETE FROM chunks WHERE id = ?1")
            .bind(id)
            .execute(&self.pool)
            .await
            .with_context(|| format!("delete_chunk_by_id({id})"))?;
        Ok(res.rows_affected())
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

    // ---- pending_approvals (schema v3) -----------------------------------

    /// Insert a fresh pending-approval row.
    ///
    /// Callers supply a UUID v4 in `row.id`; `decided_at` and `decision`
    /// must be `None` for a freshly-minted row. Re-inserting a row with an
    /// existing `id` yields a `UNIQUE` constraint error (SQL code 2067)
    /// which the caller can propagate — we don't implement upsert here.
    pub async fn insert_pending_approval(&self, row: &PendingApproval) -> Result<()> {
        sqlx::query(
            "INSERT INTO pending_approvals(id, session_key, plugin, tool, args_json, \
             requested_at, decided_at, decision) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        )
        .bind(&row.id)
        .bind(&row.session_key)
        .bind(&row.plugin)
        .bind(&row.tool)
        .bind(&row.args_json)
        .bind(&row.requested_at)
        .bind(&row.decided_at)
        .bind(&row.decision)
        .execute(&self.pool)
        .await
        .with_context(|| format!("insert_pending_approval(id={})", row.id))?;
        Ok(())
    }

    /// List approvals ordered by `requested_at ASC`. When `include_decided`
    /// is false, only rows whose `decided_at IS NULL` are returned (the
    /// queue the admin UI shows by default).
    pub async fn list_pending_approvals(
        &self,
        include_decided: bool,
    ) -> Result<Vec<PendingApproval>> {
        let sql = if include_decided {
            "SELECT id, session_key, plugin, tool, args_json, requested_at, decided_at, decision \
             FROM pending_approvals ORDER BY requested_at DESC"
        } else {
            "SELECT id, session_key, plugin, tool, args_json, requested_at, decided_at, decision \
             FROM pending_approvals WHERE decided_at IS NULL ORDER BY requested_at ASC"
        };
        let rows = sqlx::query(sql)
            .fetch_all(&self.pool)
            .await
            .context("list_pending_approvals")?;
        Ok(rows.into_iter().map(row_to_approval).collect())
    }

    /// Fetch a single row by id. Returns `None` if not present.
    pub async fn get_pending_approval(&self, id: &str) -> Result<Option<PendingApproval>> {
        let row = sqlx::query(
            "SELECT id, session_key, plugin, tool, args_json, requested_at, decided_at, decision \
             FROM pending_approvals WHERE id = ?1",
        )
        .bind(id)
        .fetch_optional(&self.pool)
        .await
        .with_context(|| format!("get_pending_approval({id})"))?;
        Ok(row.map(row_to_approval))
    }

    /// Mark a row as decided. `decision` must be one of
    /// `"approved" | "denied" | "timeout"` — callers are trusted to enforce
    /// this (the table has no CHECK constraint to keep migrations forward
    /// compatible). No-op when the id is unknown.
    pub async fn decide_approval(
        &self,
        id: &str,
        decision: &str,
        decided_at: time::OffsetDateTime,
    ) -> Result<()> {
        let decided_str = decided_at
            .format(&time::format_description::well_known::Rfc3339)
            .with_context(|| "format decided_at")?;
        sqlx::query(
            "UPDATE pending_approvals SET decided_at = ?1, decision = ?2 \
             WHERE id = ?3 AND decided_at IS NULL",
        )
        .bind(&decided_str)
        .bind(decision)
        .bind(id)
        .execute(&self.pool)
        .await
        .with_context(|| format!("decide_approval({id}, {decision})"))?;
        Ok(())
    }

    /// Delete undecided rows whose `requested_at` is strictly older than
    /// `older_than`. Returns the number of rows removed. Used by the
    /// gateway's periodic cleanup task so a long-running process doesn't
    /// accumulate orphaned prompts from crashed sessions.
    pub async fn cleanup_stale_approvals(&self, older_than: time::OffsetDateTime) -> Result<u64> {
        let cutoff = older_than
            .format(&time::format_description::well_known::Rfc3339)
            .with_context(|| "format cleanup cutoff")?;
        let res = sqlx::query(
            "DELETE FROM pending_approvals WHERE decided_at IS NULL AND requested_at < ?1",
        )
        .bind(&cutoff)
        .execute(&self.pool)
        .await
        .context("cleanup_stale_approvals")?;
        Ok(res.rows_affected())
    }

    // ---- decay + consolidation (Phase 3 W3-A) ----------------------------

    /// Apply read-time exponential decay to a list of `(chunk_id, score)`
    /// pairs. Looks up `decay_score`, `last_recalled_at`, `created_at`,
    /// and `namespace` for each id, computes the multiplicative decay
    /// factor via [`crate::decay::apply_decay`], and returns scores
    /// multiplied by the chunk's stored `decay_score` and the decay factor.
    ///
    /// Semantics:
    /// - Unknown ids are passed through unchanged (defensive — the search
    ///   path may still hold the id from a prior over-fetch).
    /// - Phase 3.1 (B-1): age zero-point is
    ///   `COALESCE(last_recalled_at, created_at)`. A chunk that was
    ///   inserted but never recalled still ages from its insertion
    ///   timestamp — without this, a sleeping row's stored
    ///   `decay_score = 1.0` would never decay and `list_promotion_candidates`
    ///   would happily flag it for consolidation despite no one ever
    ///   reading it.
    /// - `namespace = 'consolidated'` ⇒ factor = 1.0 (immune), per the
    ///   decay-module contract.
    ///
    /// Pure read — no writes. Companion [`Self::record_recall`] is what
    /// stamps `last_recalled_at` + boosts `decay_score`.
    pub async fn apply_decay_to_scored(
        &self,
        scored: &[(i64, f32)],
        cfg: &crate::decay::DecayConfig,
        now_ms: i64,
    ) -> Result<Vec<(i64, f32)>> {
        if scored.is_empty() || !cfg.enabled {
            return Ok(scored.to_vec());
        }
        let ids: Vec<i64> = scored.iter().map(|(id, _)| *id).collect();
        let placeholders = std::iter::repeat_n("?", ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT id, namespace, decay_score, last_recalled_at, created_at \
             FROM chunks WHERE id IN ({placeholders})"
        );
        let mut q = sqlx::query(&sql);
        for id in &ids {
            q = q.bind(id);
        }
        let rows = q
            .fetch_all(&self.pool)
            .await
            .context("apply_decay_to_scored: fetch metadata")?;
        // Per-id metadata: (namespace, stored_decay, age_anchor_ms). The
        // anchor is `COALESCE(last_recalled_at, created_at)` resolved
        // here in Rust rather than SQL so the legacy-NULL-created_at
        // fallback (extremely rare post-migration; only races) stays
        // visible to tracing.
        let mut meta: std::collections::HashMap<i64, (String, f32, Option<i64>)> =
            std::collections::HashMap::with_capacity(rows.len());
        for r in rows {
            let id: i64 = r.get("id");
            let ns: String = r.get("namespace");
            let stored: f32 = r.get::<f64, _>("decay_score") as f32;
            let last: Option<i64> = r.get("last_recalled_at");
            let created: Option<i64> = r.get("created_at");
            meta.insert(id, (ns, stored, last.or(created)));
        }

        let mut out: Vec<(i64, f32)> = Vec::with_capacity(scored.len());
        for (id, score) in scored {
            match meta.get(id) {
                Some((ns, stored_decay, anchor_ms)) => {
                    let age_hours = match anchor_ms {
                        Some(anchor) => {
                            let delta_ms = (now_ms - *anchor).max(0);
                            (delta_ms as f64) / 3_600_000.0
                        }
                        // Both `last_recalled_at` and `created_at` NULL
                        // ⇒ legacy row that escaped backfill (only
                        // possible if a writer raced ALTER TABLE).
                        // Treat as age=0 to avoid panicking the read
                        // path; the next migration boot will fix it.
                        None => 0.0,
                    };
                    let factor = crate::decay::apply_decay(*stored_decay, age_hours, ns, cfg);
                    out.push((*id, *score * factor));
                }
                None => out.push((*id, *score)),
            }
        }
        Ok(out)
    }

    /// Stamp `last_recalled_at = now_ms` and bump `decay_score` by
    /// `cfg.recall_boost` (capped at 1.0) for every id in `chunk_ids`.
    ///
    /// `consolidated` chunks are skipped — their score is immune by
    /// contract, and rewriting decay_score for them would just be noise.
    /// Caller drives this from the search hot path; the write should be
    /// fire-and-forget (a `tokio::spawn` wrapping this) so the read
    /// latency stays unchanged.
    pub async fn record_recall(
        &self,
        chunk_ids: &[i64],
        cfg: &crate::decay::DecayConfig,
        now_ms: i64,
    ) -> Result<u32> {
        if chunk_ids.is_empty() || !cfg.enabled {
            return Ok(0);
        }
        let placeholders = std::iter::repeat_n("?", chunk_ids.len())
            .collect::<Vec<_>>()
            .join(",");
        // Cap the boosted score at 1.0 inline. Mirrors `boosted_score` in
        // decay.rs; doing it in SQL avoids a fetch + write round-trip.
        // All `?` are unnumbered so sqlx binds positionally — mixing
        // `?N` with `?` confuses the binder.
        let sql = format!(
            "UPDATE chunks SET \
                last_recalled_at = ?, \
                decay_score = MIN(1.0, decay_score + ?) \
             WHERE id IN ({placeholders}) \
               AND namespace != ?"
        );
        let mut q = sqlx::query(&sql).bind(now_ms).bind(cfg.recall_boost as f64);
        for id in chunk_ids {
            q = q.bind(id);
        }
        q = q.bind(crate::decay::CONSOLIDATED_NAMESPACE);
        let res = q.execute(&self.pool).await.context("record_recall")?;
        Ok(res.rows_affected() as u32)
    }

    /// Promote the listed chunks into the `consolidated` namespace,
    /// stamp `consolidated_at = now_ms`, and freeze `decay_score = 1.0`.
    ///
    /// Idempotent on repeat: the second call against the same id leaves
    /// `consolidated_at` at its original value (so the audit trail
    /// preserves first-promotion time) but clamps `decay_score` to 1.0.
    /// Returns the number of rows actually promoted (rows already in
    /// `consolidated` are counted by the UPDATE rowcount but the
    /// timestamp is preserved — see the COALESCE below).
    pub async fn promote_to_consolidated(&self, chunk_ids: &[i64]) -> Result<u32> {
        if chunk_ids.is_empty() {
            return Ok(0);
        }
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        let placeholders = std::iter::repeat_n("?", chunk_ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "UPDATE chunks SET \
                namespace = ?, \
                consolidated_at = COALESCE(consolidated_at, ?), \
                decay_score = 1.0 \
             WHERE id IN ({placeholders})"
        );
        let mut q = sqlx::query(&sql)
            .bind(crate::decay::CONSOLIDATED_NAMESPACE)
            .bind(now_ms);
        for id in chunk_ids {
            q = q.bind(id);
        }
        let res = q
            .execute(&self.pool)
            .await
            .context("promote_to_consolidated")?;
        Ok(res.rows_affected() as u32)
    }

    /// Reverse of [`Self::promote_to_consolidated`]: restore the chunk's
    /// prior namespace, prior `consolidated_at`, and prior decay score.
    /// Used by the EvolutionApplier's revert path so a `consolidate_chunk`
    /// proposal can be undone byte-for-byte.
    ///
    /// Phase 3.1 (B-3): callers supply `prior_consolidated_at` so a
    /// chunk that was previously promoted, then demoted, then promoted
    /// again still keeps its first-promotion timestamp on the second
    /// demote. The previous signature unconditionally NULL'd the column,
    /// which silently dropped that audit-trail bit. Pass `None` for
    /// chunks that had never been consolidated before this revert
    /// (the common case).
    pub async fn demote_from_consolidated(
        &self,
        chunk_id: i64,
        prior_namespace: &str,
        prior_decay_score: f32,
        prior_consolidated_at: Option<i64>,
    ) -> Result<u32> {
        let res = sqlx::query(
            "UPDATE chunks SET \
                namespace = ?, \
                consolidated_at = ?, \
                decay_score = ? \
             WHERE id = ?",
        )
        .bind(prior_namespace)
        .bind(prior_consolidated_at)
        .bind(prior_decay_score as f64)
        .bind(chunk_id)
        .execute(&self.pool)
        .await
        .with_context(|| format!("demote_from_consolidated({chunk_id})"))?;
        Ok(res.rows_affected() as u32)
    }

    /// Phase 3.1: admin escape hatch for the decay sweeper. Force a
    /// chunk's `decay_score` back to 1.0 and stamp `last_recalled_at =
    /// now_ms` so the next decay tick measures age from "just now".
    ///
    /// `consolidated_at` is **preserved** — this is a forward correction
    /// (operator says "stop letting this fade"), not a rollback of any
    /// earlier consolidation. A consolidated chunk's score is already
    /// frozen at 1.0 so the call becomes a no-op stamp on
    /// `last_recalled_at`; we still run the UPDATE so the audit trail
    /// at the admin layer always sees rows_affected = 1 on a hit.
    ///
    /// Returns the rowcount (0 if `chunk_id` doesn't exist, 1 otherwise).
    pub async fn reset_chunk_decay(&self, chunk_id: i64) -> Result<u32> {
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        let res = sqlx::query(
            "UPDATE chunks SET \
                decay_score = 1.0, \
                last_recalled_at = ? \
             WHERE id = ?",
        )
        .bind(now_ms)
        .bind(chunk_id)
        .execute(&self.pool)
        .await
        .with_context(|| format!("reset_chunk_decay({chunk_id})"))?;
        Ok(res.rows_affected() as u32)
    }

    /// Return the chunks whose `decay_score >= threshold` AND whose
    /// `namespace != 'consolidated'`. Sorted decay_score-desc so the
    /// strongest candidates land first when the consolidation job caps
    /// `limit`.
    ///
    /// Phase 3.1 (B-4): a chunk is only eligible once it has actually
    /// been recalled (`last_recalled_at IS NOT NULL`) AND the recall is
    /// older than `cooling_period_hours`. Without these guards, the
    /// first cron tick after the W3-A migration would promote up to
    /// `max_promotions_per_run` random chunks — every legacy row gets
    /// `decay_score = 1.0` from the column default, and 1.0 ≥ 0.65 by
    /// definition. The cooling period also avoids consolidating a
    /// burst-recalled chunk before its score has settled.
    ///
    /// Used by the consolidation job to assemble the candidate list it
    /// then files as `memory_op` proposals — the actual promotion goes
    /// through the EvolutionApplier so the audit trail stays in lockstep
    /// with every other kb mutation.
    pub async fn list_promotion_candidates(
        &self,
        threshold: f32,
        limit: i64,
        cooling_period_hours: f64,
        now_ms: i64,
    ) -> Result<Vec<i64>> {
        if limit <= 0 {
            return Ok(Vec::new());
        }
        // Negative / non-finite cooling periods would let a buggy
        // config bypass the cold-start guard. Clamp to 0 so callers
        // always get the "must have been recalled at least once"
        // floor — operators turning the cooling period off entirely
        // is fine, but going negative is never intended.
        let cooling_ms = if cooling_period_hours.is_finite() && cooling_period_hours > 0.0 {
            (cooling_period_hours * 3_600_000.0) as i64
        } else {
            0
        };
        let cutoff_ms = now_ms.saturating_sub(cooling_ms);
        let rows = sqlx::query(
            "SELECT id FROM chunks \
             WHERE decay_score >= ? \
               AND namespace != ? \
               AND last_recalled_at IS NOT NULL \
               AND last_recalled_at <= ? \
             ORDER BY decay_score DESC, id ASC \
             LIMIT ?",
        )
        .bind(threshold as f64)
        .bind(crate::decay::CONSOLIDATED_NAMESPACE)
        .bind(cutoff_ms)
        .bind(limit)
        .fetch_all(&self.pool)
        .await
        .context("list_promotion_candidates")?;
        Ok(rows.into_iter().map(|r| r.get::<i64, _>("id")).collect())
    }

    /// Snapshot the decay-relevant columns for a single chunk. Used by
    /// the EvolutionApplier when capturing `inverse_diff` for a
    /// `consolidate_chunk` proposal so revert can restore byte-for-byte.
    /// Returns `None` when the chunk is missing.
    pub async fn get_chunk_decay_state(&self, chunk_id: i64) -> Result<Option<ChunkDecayState>> {
        let row = sqlx::query(
            "SELECT id, namespace, decay_score, consolidated_at, last_recalled_at \
             FROM chunks WHERE id = ?1",
        )
        .bind(chunk_id)
        .fetch_optional(&self.pool)
        .await
        .with_context(|| format!("get_chunk_decay_state({chunk_id})"))?;
        Ok(row.map(|r| ChunkDecayState {
            id: r.get::<i64, _>("id"),
            namespace: r.get::<String, _>("namespace"),
            decay_score: r.get::<f64, _>("decay_score") as f32,
            consolidated_at: r.get::<Option<i64>, _>("consolidated_at"),
            last_recalled_at: r.get::<Option<i64>, _>("last_recalled_at"),
        }))
    }
}

/// Snapshot of the decay-related columns on a single `chunks` row.
/// Returned by [`SqliteStore::get_chunk_decay_state`]; the
/// EvolutionApplier captures it inside `inverse_diff` so revert can
/// restore the prior namespace + decay score without guessing.
#[derive(Debug, Clone, PartialEq)]
pub struct ChunkDecayState {
    pub id: i64,
    pub namespace: String,
    pub decay_score: f32,
    pub consolidated_at: Option<i64>,
    pub last_recalled_at: Option<i64>,
}

/// Phase 3 W3-A: idempotent ALTER TABLE ADD COLUMN for the decay
/// columns on legacy v6 DBs. Uses `pragma_table_info` to skip rows
/// that are already there so a re-open of an already-migrated file
/// is a no-op.
///
/// Phase 3.1 (B-1): added `created_at` so the read-time decay age
/// has a non-NULL fallback for chunks that have never been recalled —
/// otherwise `apply_decay_to_scored` reads a NULL `last_recalled_at`
/// as age=0, which makes a freshly-inserted chunk's score stick at
/// 1.0 forever and lets `list_promotion_candidates` promote material
/// nobody has ever read.
async fn ensure_decay_columns(pool: &SqlitePool) -> Result<()> {
    // (column_name, type + default fragment)
    let columns: &[(&str, &str)] = &[
        ("decay_score", "REAL NOT NULL DEFAULT 1.0"),
        ("consolidated_at", "INTEGER"),
        ("last_recalled_at", "INTEGER"),
        // SQLite ALTER TABLE ADD COLUMN can't take a non-constant
        // DEFAULT, so we add the column nullable and backfill below.
        // Fresh DBs land via SCHEMA_SQL with the strftime-based
        // default in place; legacy v6 rows get populated with
        // `last_recalled_at` (preferred) or "now" (for chunks that
        // have never been recalled either).
        ("created_at", "INTEGER"),
    ];
    let mut backfill_created_at = false;
    for (name, type_decl) in columns {
        let exists: Option<i64> =
            sqlx::query_scalar("SELECT 1 FROM pragma_table_info('chunks') WHERE name = ?1")
                .bind(name)
                .fetch_optional(pool)
                .await
                .with_context(|| format!("probe chunks.{name}"))?;
        if exists.is_some() {
            continue;
        }
        let sql = format!("ALTER TABLE chunks ADD COLUMN {name} {type_decl}");
        sqlx::raw_sql(&sql)
            .execute(pool)
            .await
            .with_context(|| format!("ALTER TABLE chunks ADD COLUMN {name}"))?;
        if *name == "created_at" {
            backfill_created_at = true;
        }
    }
    if backfill_created_at {
        // Backfill legacy rows. `last_recalled_at` is the best proxy
        // for "when did this chunk last have a real signal"; for rows
        // that never were recalled either, anchor to "now" so the
        // decay age starts from migration time rather than 1970.
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        sqlx::query(
            "UPDATE chunks SET created_at = COALESCE(last_recalled_at, ?1) \
             WHERE created_at IS NULL",
        )
        .bind(now_ms)
        .execute(pool)
        .await
        .context("backfill chunks.created_at on legacy rows")?;
    }
    Ok(())
}

/// Phase 4 W1 4-1A: idempotent ALTER TABLE ADD COLUMN for `tenant_id`
/// on the five top-level tables. Uses `pragma_table_info` to skip
/// tables that already have the column so a re-open of an
/// already-migrated file is a no-op. Each ALTER carries
/// `NOT NULL DEFAULT 'default'`, so legacy rows are backfilled to the
/// reserved single-tenant value at ALTER time without a separate
/// UPDATE pass.
///
/// Mirrors the Phase 3.1 Tier 3 / S-2 precedent on
/// `user_traits` + `agent_persona_state`.
async fn ensure_tenant_columns(pool: &SqlitePool) -> Result<()> {
    const TABLES: &[&str] = &[
        "files",
        "chunks",
        "kv_store",
        "pending_approvals",
        "tag_nodes",
    ];
    for table in TABLES {
        let exists: Option<i64> = sqlx::query_scalar(&format!(
            "SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?1"
        ))
        .bind("tenant_id")
        .fetch_optional(pool)
        .await
        .with_context(|| format!("probe {table}.tenant_id"))?;
        if exists.is_some() {
            continue;
        }
        let sql =
            format!("ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'");
        sqlx::raw_sql(&sql)
            .execute(pool)
            .await
            .with_context(|| format!("ALTER TABLE {table} ADD COLUMN tenant_id"))?;
    }
    Ok(())
}

fn row_to_approval(r: sqlx::sqlite::SqliteRow) -> PendingApproval {
    PendingApproval {
        id: r.get::<String, _>("id"),
        session_key: r.get::<String, _>("session_key"),
        plugin: r.get::<String, _>("plugin"),
        tool: r.get::<String, _>("tool"),
        args_json: r.get::<String, _>("args_json"),
        requested_at: r.get::<String, _>("requested_at"),
        decided_at: r.get::<Option<String>, _>("decided_at"),
        decision: r.get::<Option<String>, _>("decision"),
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
        namespace: r.get::<String, _>("namespace"),
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
        for t in [
            "files",
            "chunks",
            "kv_store",
            "chunks_fts",
            "chunk_tags",
            "tag_nodes",
            "chunk_epa",
        ] {
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
            .insert_chunk(file_id, 0, "the quick brown fox jumps", None, "general")
            .await
            .unwrap();
        let target = store
            .insert_chunk(file_id, 1, "lazy dog sleeps in the sun", None, "general")
            .await
            .unwrap();
        let _ = store
            .insert_chunk(file_id, 2, "unrelated content about cats", None, "general")
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
            .insert_chunk(file_id, 0, "alpha bravo charlie", None, "general")
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
            .insert_chunk(file_id, 0, "hello world", Some(&v1), "general")
            .await
            .unwrap();
        let c2 = store
            .insert_chunk(file_id, 1, "second chunk", Some(&v2), "general")
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
            .insert_chunk(file_id, 0, "hello rebuild world", None, "general")
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
        assert!(SCHEMA_SQL.contains("pending_approvals"));
        assert!(SCHEMA_SQL.contains("chunk_tags"));
        // Sprint 9 T1 — namespace lives on chunks with a 'general' default.
        assert!(SCHEMA_SQL.contains("namespace TEXT NOT NULL DEFAULT 'general'"));
        assert!(SCHEMA_SQL.contains("idx_chunks_namespace"));
        // Sprint 9 T-B3-BE3 — hierarchical tag tree + EPA cache.
        assert!(SCHEMA_SQL.contains("tag_nodes"));
        assert!(SCHEMA_SQL.contains("tag_node_id"));
        assert!(SCHEMA_SQL.contains("chunk_epa"));
    }

    // ---- chunk_tags -----------------------------------------------------

    async fn seed_tagged_chunks(store: &SqliteStore) -> (i64, i64, i64) {
        // Returns (chunk_a, chunk_b, chunk_c):
        //   a → tags ["rust", "backend"]
        //   b → tags ["rust", "frontend"]
        //   c → no tags
        let file_id = store
            .insert_file("t.md", "default", "h", 0, 0)
            .await
            .unwrap();
        let a = store
            .insert_chunk(file_id, 0, "rust backend content", None, "general")
            .await
            .unwrap();
        let b = store
            .insert_chunk(file_id, 1, "rust frontend content", None, "general")
            .await
            .unwrap();
        let c = store
            .insert_chunk(file_id, 2, "untagged note", None, "general")
            .await
            .unwrap();
        store.insert_tag(a, "rust").await.unwrap();
        store.insert_tag(a, "backend").await.unwrap();
        store.insert_tag(b, "rust").await.unwrap();
        store.insert_tag(b, "frontend").await.unwrap();
        (a, b, c)
    }

    #[tokio::test]
    async fn insert_and_get_tags_roundtrip() {
        let (store, _tmp) = fresh_store().await;
        let (a, _b, c) = seed_tagged_chunks(&store).await;
        assert_eq!(store.get_tags(a).await.unwrap(), vec!["backend", "rust"]);
        assert_eq!(store.get_tags(c).await.unwrap(), Vec::<String>::new());
        // Idempotency.
        store.insert_tag(a, "rust").await.unwrap();
        assert_eq!(store.get_tags(a).await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn count_files_and_tags() {
        let (store, _tmp) = fresh_store().await;
        assert_eq!(store.count_files().await.unwrap(), 0);
        assert_eq!(store.count_tags().await.unwrap(), 0);
        seed_tagged_chunks(&store).await;
        assert_eq!(store.count_files().await.unwrap(), 1);
        // distinct tags: rust, backend, frontend
        assert_eq!(store.count_tags().await.unwrap(), 3);
    }

    #[tokio::test]
    async fn search_bm25_with_filter_restricts_hits() {
        let (store, _tmp) = fresh_store().await;
        let (a, _b, _c) = seed_tagged_chunks(&store).await;
        // No filter ⇒ picks up both "rust ..." chunks.
        let hits = store
            .search_bm25_with_filter("rust", 10, None)
            .await
            .unwrap();
        assert_eq!(hits.len(), 2);
        // Whitelist only chunk a.
        let hits = store
            .search_bm25_with_filter("rust", 10, Some(&[a]))
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, a);
        // Empty whitelist ⇒ empty.
        let hits = store
            .search_bm25_with_filter("rust", 10, Some(&[]))
            .await
            .unwrap();
        assert!(hits.is_empty());
    }

    // ---- pending_approvals ------------------------------------------------

    fn sample_approval(id: &str, session: &str) -> PendingApproval {
        PendingApproval {
            id: id.into(),
            session_key: session.into(),
            plugin: "file-ops".into(),
            tool: "write".into(),
            args_json: r#"{"path":"a.md"}"#.into(),
            requested_at: "2026-04-20T06:00:00Z".into(),
            decided_at: None,
            decision: None,
        }
    }

    #[tokio::test]
    async fn pending_approvals_insert_and_list_roundtrip() {
        let (store, _tmp) = fresh_store().await;
        store
            .insert_pending_approval(&sample_approval("apv_a", "sess_a"))
            .await
            .unwrap();
        store
            .insert_pending_approval(&sample_approval("apv_b", "sess_b"))
            .await
            .unwrap();

        let undecided = store.list_pending_approvals(false).await.unwrap();
        assert_eq!(undecided.len(), 2);
        assert_eq!(undecided[0].id, "apv_a");
        assert_eq!(undecided[1].id, "apv_b");

        let one = store.get_pending_approval("apv_a").await.unwrap().unwrap();
        assert_eq!(one.plugin, "file-ops");
        assert!(store
            .get_pending_approval("missing")
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn decide_approval_moves_row_out_of_undecided_view() {
        let (store, _tmp) = fresh_store().await;
        store
            .insert_pending_approval(&sample_approval("apv_x", "sess"))
            .await
            .unwrap();
        store
            .decide_approval("apv_x", "approved", time::OffsetDateTime::now_utc())
            .await
            .unwrap();

        let undecided = store.list_pending_approvals(false).await.unwrap();
        assert!(undecided.is_empty());
        let all = store.list_pending_approvals(true).await.unwrap();
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].decision.as_deref(), Some("approved"));
        assert!(all[0].decided_at.is_some());
    }

    #[tokio::test]
    async fn cleanup_stale_approvals_drops_only_old_undecided() {
        let (store, _tmp) = fresh_store().await;
        // Old undecided — gets pruned.
        let mut old = sample_approval("apv_old", "sess");
        old.requested_at = "2020-01-01T00:00:00Z".into();
        store.insert_pending_approval(&old).await.unwrap();
        // Recent undecided — kept.
        let mut recent = sample_approval("apv_new", "sess");
        recent.requested_at = "2099-01-01T00:00:00Z".into();
        store.insert_pending_approval(&recent).await.unwrap();
        // Old decided — kept (history).
        let mut decided = sample_approval("apv_done", "sess");
        decided.requested_at = "2020-01-01T00:00:00Z".into();
        store.insert_pending_approval(&decided).await.unwrap();
        store
            .decide_approval("apv_done", "approved", time::OffsetDateTime::now_utc())
            .await
            .unwrap();

        let removed = store
            .cleanup_stale_approvals(time::OffsetDateTime::now_utc())
            .await
            .unwrap();
        assert_eq!(removed, 1);
        let all = store.list_pending_approvals(true).await.unwrap();
        let ids: Vec<_> = all.iter().map(|a| a.id.as_str()).collect();
        assert!(ids.contains(&"apv_new"));
        assert!(ids.contains(&"apv_done"));
        assert!(!ids.contains(&"apv_old"));
    }

    // ---- decay + consolidation (Phase 3 W3-A) ----------------------------

    use crate::decay::{DecayConfig, CONSOLIDATED_NAMESPACE};

    async fn seed_decayable_chunk(store: &SqliteStore, namespace: &str) -> i64 {
        let path = format!(
            "/tmp/{namespace}-{}.md",
            time::OffsetDateTime::now_utc().unix_timestamp_nanos()
        );
        let file_id = store
            .insert_file(&path, "default", "h", 0, 0)
            .await
            .unwrap();
        store
            .insert_chunk(file_id, 0, "decayable content", None, namespace)
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn schema_carries_decay_columns_on_fresh_db() {
        // Freshly opened DB exposes the four decay-related columns
        // (Phase 3.1 added `created_at` so age has a non-NULL fallback
        // when last_recalled_at IS NULL). Migration path doesn't fire
        // (columns already in SCHEMA_SQL) but the assertion still pins
        // the public surface.
        let (store, _tmp) = fresh_store().await;
        let row = sqlx::query(
            "SELECT name FROM pragma_table_info('chunks') \
             WHERE name IN ('decay_score', 'consolidated_at', 'last_recalled_at', 'created_at') \
             ORDER BY name",
        )
        .fetch_all(store.pool())
        .await
        .unwrap();
        let names: Vec<String> = row
            .into_iter()
            .map(|r| r.get::<String, _>("name"))
            .collect();
        assert_eq!(
            names,
            vec![
                "consolidated_at".to_string(),
                "created_at".to_string(),
                "decay_score".to_string(),
                "last_recalled_at".to_string()
            ]
        );
    }

    /// Phase 4 W1 4-1A: fresh DB lands with `tenant_id` on all five
    /// migrated tables and the tenant-aware indexes from
    /// TENANT_INDEXES_SQL are present.
    #[tokio::test]
    async fn fresh_db_has_phase4_tenant_columns_and_indexes() {
        let (store, _tmp) = fresh_store().await;
        for table in [
            "files",
            "chunks",
            "kv_store",
            "pending_approvals",
            "tag_nodes",
        ] {
            let exists: Option<i64> = sqlx::query_scalar(&format!(
                "SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?1"
            ))
            .bind("tenant_id")
            .fetch_optional(store.pool())
            .await
            .unwrap();
            assert!(exists.is_some(), "{table}.tenant_id missing on fresh DB");
        }

        for idx in [
            "idx_files_tenant_diary",
            "idx_chunks_tenant_namespace",
            "idx_kv_tenant",
            "idx_tag_nodes_tenant_path",
            "idx_pending_approvals_tenant_undecided",
        ] {
            let row: Option<String> =
                sqlx::query_scalar("SELECT name FROM sqlite_master WHERE type='index' AND name=?1")
                    .bind(idx)
                    .fetch_optional(store.pool())
                    .await
                    .unwrap();
            assert_eq!(row.as_deref(), Some(idx), "missing index {idx}");
        }

        // Pre-tenant `idx_pending_approvals_undecided` should have been
        // dropped by TENANT_INDEXES_SQL.
        let stale: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pending_approvals_undecided'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert!(
            stale.is_none(),
            "old idx_pending_approvals_undecided should be gone"
        );
    }

    /// Phase 4 W1 4-1A: a legacy DB created with the pre-Phase-4
    /// schema must converge to the tenant-aware shape when opened
    /// through `SqliteStore::open`. Bootstrap a separate sqlite file
    /// directly via `SqliteConnectOptions` (no FTS triggers, no FK
    /// enforcement headaches), close it, then reopen via the
    /// production path and assert tenant_id + tenant indexes appear.
    #[tokio::test]
    async fn open_migrates_legacy_db_to_phase4_tenant_shape() {
        use sqlx::sqlite::SqliteConnectOptions;
        use std::str::FromStr;

        let tmp = TempDir::new().expect("tempdir");
        let path = tmp.path().join("kb.sqlite");
        let url = format!("sqlite://{}", path.display());

        // Bootstrap a legacy v6 (post-W3-A, pre-Phase-4) shape: tables
        // without tenant_id, plus the legacy `idx_pending_approvals_undecided`
        // partial index. Use a separate connection so we close it cleanly
        // before reopening through `open`.
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
                r#"CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    diary_name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    mtime INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    updated_at INTEGER
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    vector BLOB,
                    namespace TEXT NOT NULL DEFAULT 'general',
                    decay_score REAL NOT NULL DEFAULT 1.0,
                    consolidated_at INTEGER,
                    last_recalled_at INTEGER,
                    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );
                CREATE TABLE kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    vector BLOB
                );
                CREATE TABLE pending_approvals (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    plugin TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decision TEXT
                );
                CREATE INDEX idx_pending_approvals_undecided
                    ON pending_approvals(decided_at) WHERE decided_at IS NULL;
                CREATE TABLE tag_nodes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id   INTEGER REFERENCES tag_nodes(id) ON DELETE CASCADE,
                    name        TEXT NOT NULL,
                    path        TEXT NOT NULL UNIQUE,
                    depth       INTEGER NOT NULL,
                    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                "#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Open through the production path → tenant migrations apply.
        let store = SqliteStore::open(&path).await.unwrap();

        for table in [
            "files",
            "chunks",
            "kv_store",
            "pending_approvals",
            "tag_nodes",
        ] {
            let cnt: i64 = sqlx::query_scalar(&format!(
                "SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name='tenant_id'"
            ))
            .fetch_one(store.pool())
            .await
            .unwrap();
            assert_eq!(cnt, 1, "open() must add {table}.tenant_id on legacy DB");
        }

        for idx in [
            "idx_files_tenant_diary",
            "idx_chunks_tenant_namespace",
            "idx_kv_tenant",
            "idx_tag_nodes_tenant_path",
            "idx_pending_approvals_tenant_undecided",
        ] {
            let row: Option<String> =
                sqlx::query_scalar("SELECT name FROM sqlite_master WHERE type='index' AND name=?1")
                    .bind(idx)
                    .fetch_optional(store.pool())
                    .await
                    .unwrap();
            assert_eq!(row.as_deref(), Some(idx), "missing index {idx}");
        }

        // Legacy partial index swapped out by TENANT_INDEXES_SQL.
        let stale: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pending_approvals_undecided'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert!(
            stale.is_none(),
            "legacy idx_pending_approvals_undecided should be dropped"
        );

        // Idempotent reopen: production path on already-migrated DB
        // must be a clean no-op.
        drop(store);
        let _store2 = SqliteStore::open(&path).await.unwrap();
    }

    #[tokio::test]
    async fn ensure_decay_columns_is_idempotent() {
        // Running `ensure_decay_columns` against a DB that already has
        // them is a no-op (the pragma probe short-circuits each ADD).
        let (store, _tmp) = fresh_store().await;
        ensure_decay_columns(store.pool()).await.unwrap();
        ensure_decay_columns(store.pool()).await.unwrap();
    }

    #[tokio::test]
    async fn ensure_decay_columns_backfills_legacy_v6_db() {
        // Simulate a pre-W3-A v6 file: drop the decay columns, re-run
        // `ensure_decay_columns`, confirm they reappear (Phase 3.1
        // added `created_at` to the set, so we now check four).
        let (store, _tmp) = fresh_store().await;
        sqlx::raw_sql(
            "DROP TABLE chunks_fts; \
             CREATE TABLE chunks_legacy AS SELECT id, file_id, chunk_index, content, vector, namespace FROM chunks; \
             DROP TABLE chunks; \
             CREATE TABLE chunks ( \
                 id INTEGER PRIMARY KEY AUTOINCREMENT, \
                 file_id INTEGER NOT NULL, \
                 chunk_index INTEGER NOT NULL, \
                 content TEXT NOT NULL, \
                 vector BLOB, \
                 namespace TEXT NOT NULL DEFAULT 'general', \
                 FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE \
             ); \
             INSERT INTO chunks SELECT * FROM chunks_legacy; \
             DROP TABLE chunks_legacy;",
        )
        .execute(store.pool())
        .await
        .unwrap();
        let cnt: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pragma_table_info('chunks') \
             WHERE name IN ('decay_score', 'consolidated_at', 'last_recalled_at', 'created_at')",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(cnt, 0, "fixture should have stripped the columns");

        ensure_decay_columns(store.pool()).await.unwrap();

        let cnt: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pragma_table_info('chunks') \
             WHERE name IN ('decay_score', 'consolidated_at', 'last_recalled_at', 'created_at')",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(cnt, 4, "all four columns added");
    }

    /// Phase 3.1 (B-1): legacy rows that existed before the migration
    /// must end up with a sane `created_at` so the decay age zero-point
    /// has a non-NULL fallback. The backfill prefers `last_recalled_at`
    /// when present; otherwise it stamps "now" (we anchor below to a
    /// known timestamp so the assertion is deterministic).
    #[tokio::test]
    async fn ensure_decay_columns_backfills_created_at_on_legacy_rows() {
        let (store, _tmp) = fresh_store().await;
        // Strip created_at while keeping the other Phase 3 W3-A columns
        // — that's the realistic upgrade scenario from the W3-A drop.
        sqlx::raw_sql(
            "DROP TABLE chunks_fts; \
             CREATE TABLE chunks_legacy AS SELECT id, file_id, chunk_index, content, vector, namespace, decay_score, consolidated_at, last_recalled_at FROM chunks; \
             DROP TABLE chunks; \
             CREATE TABLE chunks ( \
                 id INTEGER PRIMARY KEY AUTOINCREMENT, \
                 file_id INTEGER NOT NULL, \
                 chunk_index INTEGER NOT NULL, \
                 content TEXT NOT NULL, \
                 vector BLOB, \
                 namespace TEXT NOT NULL DEFAULT 'general', \
                 decay_score REAL NOT NULL DEFAULT 1.0, \
                 consolidated_at INTEGER, \
                 last_recalled_at INTEGER, \
                 FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE \
             ); \
             INSERT INTO chunks SELECT * FROM chunks_legacy; \
             DROP TABLE chunks_legacy;",
        )
        .execute(store.pool())
        .await
        .unwrap();
        // Two legacy rows: one with last_recalled_at, one without.
        sqlx::query(
            "INSERT INTO files(path, diary_name, checksum, mtime, size) \
             VALUES ('/legacy.md', 'd', 'h', 0, 0)",
        )
        .execute(store.pool())
        .await
        .unwrap();
        sqlx::query(
            "INSERT INTO chunks(file_id, chunk_index, content, namespace, last_recalled_at) \
             VALUES (1, 0, 'recalled', 'general', 12345), \
                    (1, 1, 'never', 'general', NULL)",
        )
        .execute(store.pool())
        .await
        .unwrap();

        ensure_decay_columns(store.pool()).await.unwrap();

        let row: (i64, Option<i64>) =
            sqlx::query_as("SELECT id, created_at FROM chunks WHERE chunk_index = 0")
                .fetch_one(store.pool())
                .await
                .unwrap();
        assert_eq!(
            row.1,
            Some(12345),
            "recalled row: created_at = last_recalled_at"
        );

        let row: (i64, Option<i64>) =
            sqlx::query_as("SELECT id, created_at FROM chunks WHERE chunk_index = 1")
                .fetch_one(store.pool())
                .await
                .unwrap();
        assert!(
            row.1.is_some(),
            "never-recalled row: created_at filled with now()"
        );
    }

    /// Phase 3.1 (B-1): the read-time decay path must use `created_at`
    /// as the age zero-point when `last_recalled_at IS NULL`. Without
    /// this, a never-recalled chunk's score sticks at 1.0 forever and
    /// the "类人 forgetting" promise breaks for fresh material.
    #[tokio::test]
    async fn apply_decay_to_scored_uses_created_at_when_never_recalled() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = crate::decay::DecayConfig::default();
        // Anchor created_at to "one half-life ago" so the expected
        // factor is exactly 0.5.
        let half_life_ms = (cfg.half_life_hours * 3_600_000.0) as i64;
        let now_ms = 10_000_000_000_i64;
        sqlx::query("UPDATE chunks SET created_at = ?1, last_recalled_at = NULL WHERE id = ?2")
            .bind(now_ms - half_life_ms)
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();
        let out = store
            .apply_decay_to_scored(&[(id, 0.8)], &cfg, now_ms)
            .await
            .unwrap();
        assert!(
            (out[0].1 - 0.4).abs() < 1e-3,
            "expected ~0.4 (0.8 * 0.5), got {}",
            out[0].1
        );
    }

    #[tokio::test]
    async fn fresh_chunks_have_default_decay_state() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.decay_score, 1.0);
        assert!(state.consolidated_at.is_none());
        assert!(state.last_recalled_at.is_none());
        assert_eq!(state.namespace, "general");
    }

    #[tokio::test]
    async fn apply_decay_to_scored_passes_through_when_never_recalled() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = DecayConfig::default();
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        let scored = vec![(id, 0.8_f32)];
        let out = store
            .apply_decay_to_scored(&scored, &cfg, now_ms)
            .await
            .unwrap();
        assert_eq!(out.len(), 1);
        // last_recalled_at IS NULL → age = 0 → factor = 1.0;
        // stored decay_score = 1.0 → score unchanged.
        assert!((out[0].1 - 0.8).abs() < 1e-5, "got {}", out[0].1);
    }

    #[tokio::test]
    async fn apply_decay_to_scored_halves_at_one_half_life() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = DecayConfig::default();
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        let half_life_ms = (cfg.half_life_hours * 3_600_000.0) as i64;
        let recalled_at = now_ms - half_life_ms;
        sqlx::query("UPDATE chunks SET last_recalled_at = ?1 WHERE id = ?2")
            .bind(recalled_at)
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();
        let out = store
            .apply_decay_to_scored(&[(id, 0.8)], &cfg, now_ms)
            .await
            .unwrap();
        // 0.8 (BM25 score) * 1.0 (stored decay_score) * 0.5 (half-life
        // factor) = 0.4
        assert!((out[0].1 - 0.4).abs() < 1e-3, "got {}", out[0].1);
    }

    #[tokio::test]
    async fn apply_decay_to_scored_skips_consolidated_namespace() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, CONSOLIDATED_NAMESPACE).await;
        let cfg = DecayConfig::default();
        let now_ms = (time::OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
        let recalled_at = now_ms - (cfg.half_life_hours as i64 * 100 * 3_600_000);
        sqlx::query("UPDATE chunks SET last_recalled_at = ?1 WHERE id = ?2")
            .bind(recalled_at)
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();
        let out = store
            .apply_decay_to_scored(&[(id, 0.8)], &cfg, now_ms)
            .await
            .unwrap();
        assert!((out[0].1 - 0.8).abs() < 1e-5);
    }

    #[tokio::test]
    async fn apply_decay_disabled_returns_input_unchanged() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = DecayConfig {
            enabled: false,
            ..Default::default()
        };
        let out = store
            .apply_decay_to_scored(&[(id, 0.8)], &cfg, 0)
            .await
            .unwrap();
        assert_eq!(out[0], (id, 0.8));
    }

    #[tokio::test]
    async fn apply_decay_unknown_id_passes_through() {
        let (store, _tmp) = fresh_store().await;
        let cfg = DecayConfig::default();
        let out = store
            .apply_decay_to_scored(&[(99_999, 0.7)], &cfg, 0)
            .await
            .unwrap();
        assert_eq!(out, vec![(99_999, 0.7)]);
    }

    #[tokio::test]
    async fn record_recall_stamps_timestamp_and_boosts_score() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        sqlx::query("UPDATE chunks SET decay_score = 0.4 WHERE id = ?1")
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();

        let cfg = DecayConfig::default(); // recall_boost = 0.3
        let now_ms = 12_345_000_i64;
        let n = store.record_recall(&[id], &cfg, now_ms).await.unwrap();
        assert_eq!(n, 1);

        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.last_recalled_at, Some(now_ms));
        assert!(
            (state.decay_score - 0.7).abs() < 1e-5,
            "got {}",
            state.decay_score
        );
    }

    #[tokio::test]
    async fn record_recall_caps_decay_score_at_one() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = DecayConfig::default();
        store.record_recall(&[id], &cfg, 1_000).await.unwrap();
        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.decay_score, 1.0);
    }

    #[tokio::test]
    async fn record_recall_skips_consolidated_chunks() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, CONSOLIDATED_NAMESPACE).await;
        let cfg = DecayConfig::default();
        let n = store.record_recall(&[id], &cfg, 999).await.unwrap();
        assert_eq!(n, 0, "consolidated chunks must not be touched");
        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert!(state.last_recalled_at.is_none());
    }

    #[tokio::test]
    async fn record_recall_disabled_is_noop() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let cfg = DecayConfig {
            enabled: false,
            ..Default::default()
        };
        let n = store.record_recall(&[id], &cfg, 999).await.unwrap();
        assert_eq!(n, 0);
        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert!(state.last_recalled_at.is_none());
    }

    #[tokio::test]
    async fn promote_to_consolidated_flips_namespace_and_stamps_time() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let n = store.promote_to_consolidated(&[id]).await.unwrap();
        assert_eq!(n, 1);
        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.namespace, CONSOLIDATED_NAMESPACE);
        assert_eq!(state.decay_score, 1.0);
        assert!(state.consolidated_at.is_some());
    }

    #[tokio::test]
    async fn promote_to_consolidated_preserves_first_promotion_time() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        store.promote_to_consolidated(&[id]).await.unwrap();
        let first = store
            .get_chunk_decay_state(id)
            .await
            .unwrap()
            .unwrap()
            .consolidated_at
            .unwrap();
        store.promote_to_consolidated(&[id]).await.unwrap();
        let after = store
            .get_chunk_decay_state(id)
            .await
            .unwrap()
            .unwrap()
            .consolidated_at
            .unwrap();
        assert_eq!(first, after, "consolidated_at must be sticky");
    }

    /// Helper: stamp `last_recalled_at` so a chunk satisfies the
    /// Phase 3.1 (B-4) cooling-period guard. Tests that pre-date that
    /// guard used to assume every fresh chunk was eligible — wiring the
    /// stamp explicitly keeps the assertion intent obvious.
    async fn stamp_recalled(store: &SqliteStore, id: i64, recalled_ms: i64) {
        sqlx::query("UPDATE chunks SET last_recalled_at = ?1 WHERE id = ?2")
            .bind(recalled_ms)
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn list_promotion_candidates_filters_by_threshold_and_namespace() {
        let (store, _tmp) = fresh_store().await;
        let high = seed_decayable_chunk(&store, "general").await;
        let low = seed_decayable_chunk(&store, "general").await;
        let consolidated = seed_decayable_chunk(&store, "general").await;

        sqlx::query("UPDATE chunks SET decay_score = 0.4 WHERE id = ?1")
            .bind(low)
            .execute(store.pool())
            .await
            .unwrap();
        store
            .promote_to_consolidated(&[consolidated])
            .await
            .unwrap();

        // Stamp recalls in the distant past so cooling period (24h
        // default) is satisfied for every candidate.
        let recalled = 1_000_i64;
        let now_ms = recalled + (24 + 1) * 3_600_000;
        for id in [high, low, consolidated] {
            stamp_recalled(&store, id, recalled).await;
        }
        let ids = store
            .list_promotion_candidates(0.65, 50, 24.0, now_ms)
            .await
            .unwrap();
        assert_eq!(ids, vec![high]);
    }

    #[tokio::test]
    async fn list_promotion_candidates_respects_limit() {
        let (store, _tmp) = fresh_store().await;
        let mut ids = Vec::new();
        for _ in 0..5 {
            ids.push(seed_decayable_chunk(&store, "general").await);
        }
        let recalled = 1_000_i64;
        let now_ms = recalled + (24 + 1) * 3_600_000;
        for id in &ids {
            stamp_recalled(&store, *id, recalled).await;
        }
        let out = store
            .list_promotion_candidates(0.0, 3, 24.0, now_ms)
            .await
            .unwrap();
        assert_eq!(out.len(), 3);
    }

    /// Phase 3.1 (B-4): chunks that have never been recalled must NOT
    /// be promotion candidates, no matter how high their default
    /// decay_score. Pins the cold-start guard against the post-migration
    /// scenario where every legacy row sits at decay_score=1.0.
    #[tokio::test]
    async fn list_promotion_candidates_excludes_never_recalled() {
        let (store, _tmp) = fresh_store().await;
        let _id = seed_decayable_chunk(&store, "general").await;
        // No stamp_recalled — last_recalled_at IS NULL.
        let out = store
            .list_promotion_candidates(0.0, 50, 24.0, 1_000_000_000_000)
            .await
            .unwrap();
        assert!(
            out.is_empty(),
            "never-recalled chunks must not be promotion candidates, got {out:?}"
        );
    }

    /// Phase 3.1 (B-4): chunks recalled inside the cooling period must
    /// stay out of the candidate list. Lets a burst-read chunk's score
    /// settle before consolidation locks it in.
    #[tokio::test]
    async fn list_promotion_candidates_respects_cooling_period() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        let now_ms: i64 = 100 * 3_600_000;
        // Recalled 5 hours ago, cooling = 24h ⇒ ineligible.
        stamp_recalled(&store, id, now_ms - 5 * 3_600_000).await;
        let out = store
            .list_promotion_candidates(0.0, 50, 24.0, now_ms)
            .await
            .unwrap();
        assert!(out.is_empty());

        // Recalled 25h ago ⇒ now eligible.
        stamp_recalled(&store, id, now_ms - 25 * 3_600_000).await;
        let out = store
            .list_promotion_candidates(0.0, 50, 24.0, now_ms)
            .await
            .unwrap();
        assert_eq!(out, vec![id]);
    }

    #[tokio::test]
    async fn demote_from_consolidated_restores_namespace_and_score() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        sqlx::query("UPDATE chunks SET decay_score = 0.55 WHERE id = ?1")
            .bind(id)
            .execute(store.pool())
            .await
            .unwrap();
        let prior = store.get_chunk_decay_state(id).await.unwrap().unwrap();

        store.promote_to_consolidated(&[id]).await.unwrap();
        let n = store
            .demote_from_consolidated(
                id,
                &prior.namespace,
                prior.decay_score,
                prior.consolidated_at,
            )
            .await
            .unwrap();
        assert_eq!(n, 1);

        let state = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.namespace, "general");
        assert!(state.consolidated_at.is_none());
        assert!((state.decay_score - 0.55).abs() < 1e-5);
    }

    /// Phase 3.1 (B-3): a chunk that was previously promoted, demoted,
    /// then promoted again must keep its original first-promotion
    /// timestamp on the second demote. Pins the new
    /// `prior_consolidated_at` parameter end-to-end at the SQL layer.
    #[tokio::test]
    async fn demote_from_consolidated_preserves_prior_consolidated_at() {
        let (store, _tmp) = fresh_store().await;
        let id = seed_decayable_chunk(&store, "general").await;
        // First promotion stamps consolidated_at.
        store.promote_to_consolidated(&[id]).await.unwrap();
        let first_promotion = store
            .get_chunk_decay_state(id)
            .await
            .unwrap()
            .unwrap()
            .consolidated_at
            .unwrap();
        // Demote, then re-promote — second promotion COALESCEs to the
        // existing consolidated_at, no change. Demote a second time
        // and feed the captured first_promotion ts back in.
        store
            .demote_from_consolidated(id, "general", 1.0, None)
            .await
            .unwrap();
        store.promote_to_consolidated(&[id]).await.unwrap();
        store
            .demote_from_consolidated(id, "general", 1.0, Some(first_promotion))
            .await
            .unwrap();
        let after = store.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(
            after.consolidated_at,
            Some(first_promotion),
            "prior_consolidated_at must round-trip"
        );
    }
}
