//! SQLite-backed [`SessionStore`] — persists chat message histories keyed by
//! `session_key` in `<data_dir>/sessions.sqlite`.
//!
//! Schema (single table, one row per message):
//!
//! ```sql
//! CREATE TABLE IF NOT EXISTS sessions (
//!     session_key TEXT NOT NULL,
//!     seq INTEGER NOT NULL,
//!     role TEXT NOT NULL,
//!     content TEXT NOT NULL,
//!     tool_call_id TEXT,
//!     tool_calls_json TEXT,
//!     ts TEXT NOT NULL,
//!     PRIMARY KEY (session_key, seq)
//! );
//! CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);
//! ```
//!
//! The primary key `(session_key, seq)` serves double duty: it's a unique index
//! so `append` can compute the next `seq` with a `MAX()+1` scan over the
//! leftmost key, *and* the `idx_sessions_key` secondary index accelerates the
//! scan when multiple concurrent sessions share the table.
//!
//! `ts` is stored as RFC3339 text so sqlite's human-readable dump remains
//! useful for debugging; `tool_calls_json` carries the OpenAI tool_calls array
//! verbatim as a JSON string (nullable; only assistant-role messages that
//! requested tool execution populate it).

use std::path::Path;
use std::str::FromStr;

use async_trait::async_trait;
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Row, SqlitePool};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use crate::error::CorlinmanError;
use crate::session::{SessionMessage, SessionRole, SessionStore, SessionSummary};

/// Full DDL applied on open. Idempotent — safe against an existing file.
pub const SCHEMA_SQL: &str = r#"
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
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);
"#;

/// Indexes that reference the Phase 4 W1 4-1A `tenant_id` column. Run
/// *after* `ensure_tenant_column` so the column exists on legacy DBs
/// before SQLite resolves index column names.
const TENANT_INDEX_SQL: &str = r#"
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_key
    ON sessions(tenant_id, session_key, seq);
"#;

/// Storage error helper — wrap a sqlx error into `CorlinmanError::Storage` with
/// a short operation tag so logs can distinguish failing queries.
fn storage<E: std::fmt::Display>(op: &str, e: E) -> CorlinmanError {
    CorlinmanError::Storage(format!("sessions {op}: {e}"))
}

/// Phase 4 W1 4-1A: idempotent ALTER TABLE ADD COLUMN for `tenant_id`
/// on the single `sessions` table. Probes via pragma_table_info to skip
/// the ALTER on already-migrated DBs. Mirrors the
/// `ensure_decay_columns` / `ensure_tenant_columns` pattern used by
/// `corlinman-vector`.
async fn ensure_tenant_column(pool: &SqlitePool) -> Result<(), CorlinmanError> {
    let exists: Option<i64> =
        sqlx::query_scalar("SELECT 1 FROM pragma_table_info('sessions') WHERE name = ?1")
            .bind("tenant_id")
            .fetch_optional(pool)
            .await
            .map_err(|e| storage("probe_tenant", e))?;
    if exists.is_none() {
        sqlx::raw_sql("ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            .execute(pool)
            .await
            .map_err(|e| storage("alter_tenant", e))?;
    }
    Ok(())
}

/// SQLite-backed session store. Cheap to clone; internally holds a pooled
/// connection.
#[derive(Debug, Clone)]
pub struct SqliteSessionStore {
    pool: SqlitePool,
}

impl SqliteSessionStore {
    /// Open (or create) the sessions database at `path`.
    ///
    /// Opens with WAL + `synchronous=NORMAL` for write throughput and applies
    /// [`SCHEMA_SQL`] so callers never have to run migrations by hand.
    pub async fn open(path: &Path) -> Result<Self, CorlinmanError> {
        let url = format!("sqlite://{}", path.display());
        let options = SqliteConnectOptions::from_str(&url)
            .map_err(|e| storage("parse_url", e))?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .busy_timeout(std::time::Duration::from_secs(5));

        let pool = SqlitePoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .map_err(|e| storage("connect", e))?;

        sqlx::raw_sql(SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(|e| storage("apply_schema", e))?;

        // Phase 4 W1 4-1A: idempotent tenant_id column add for legacy
        // pre-tenant DBs. Probes via pragma_table_info; on miss, ALTER
        // adds the column with `NOT NULL DEFAULT 'default'` so legacy
        // rows backfill at ALTER time. Re-open of an already-migrated
        // file is a no-op.
        ensure_tenant_column(&pool).await?;

        // Indexes referencing `tenant_id` must run after the column
        // exists on legacy DBs; SQLite resolves index column names at
        // create time.
        sqlx::raw_sql(TENANT_INDEX_SQL)
            .execute(&pool)
            .await
            .map_err(|e| storage("apply_tenant_index", e))?;

        Ok(Self { pool })
    }

    /// Borrow the pool (tests only).
    #[cfg(test)]
    pub(crate) fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    /// Phase 4 W2 4-2D: aggregate per-session metadata for the admin
    /// sessions list route. Returns one [`SessionSummary`] per distinct
    /// `session_key`, ordered by `MAX(ts) DESC` so the UI's most-recent
    /// session shows up at the top without a follow-up sort.
    ///
    /// `ts` is stored as RFC-3339 text; we parse `MAX(ts)` per row into
    /// unix-ms here so the wire shape can stay numeric (sortable, cheap
    /// to compare). Rows whose `ts` fails to parse are skipped — the
    /// SQLite column is `NOT NULL` and every writer goes through
    /// [`SessionMessage`], so this case implies an externally-corrupted
    /// row and the operator should run `corlinman doctor` rather than
    /// see a silent zero in the UI.
    pub async fn list_sessions(&self) -> Result<Vec<SessionSummary>, CorlinmanError> {
        let rows = sqlx::query(
            "SELECT session_key, MAX(ts) AS last_ts, COUNT(*) AS msg_count \
             FROM sessions \
             GROUP BY session_key \
             ORDER BY MAX(ts) DESC",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(|e| storage("list_sessions", e))?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let session_key: String = row.get("session_key");
            let last_ts: String = row.get("last_ts");
            let msg_count: i64 = row.get("msg_count");
            let last_message_at_ms = match OffsetDateTime::parse(&last_ts, &Rfc3339) {
                Ok(dt) => (dt.unix_timestamp_nanos() / 1_000_000) as i64,
                Err(e) => {
                    tracing::warn!(
                        session_key = %session_key,
                        last_ts = %last_ts,
                        error = %e,
                        "list_sessions: skipping row with unparseable ts",
                    );
                    continue;
                }
            };
            out.push(SessionSummary {
                session_key,
                last_message_at_ms,
                message_count: msg_count,
            });
        }
        Ok(out)
    }
}

#[async_trait]
impl SessionStore for SqliteSessionStore {
    async fn load(&self, session_key: &str) -> Result<Vec<SessionMessage>, CorlinmanError> {
        let rows = sqlx::query(
            "SELECT role, content, tool_call_id, tool_calls_json, ts \
             FROM sessions WHERE session_key = ?1 ORDER BY seq ASC",
        )
        .bind(session_key)
        .fetch_all(&self.pool)
        .await
        .map_err(|e| storage("load", e))?;

        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let role: String = r.get("role");
            let content: String = r.get("content");
            let tool_call_id: Option<String> = r.get("tool_call_id");
            let tool_calls_json: Option<String> = r.get("tool_calls_json");
            let ts_raw: String = r.get("ts");
            let ts = OffsetDateTime::parse(&ts_raw, &Rfc3339)
                .unwrap_or_else(|_| OffsetDateTime::now_utc());
            let tool_calls = tool_calls_json
                .as_deref()
                .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok());
            out.push(SessionMessage {
                role: SessionRole::from_str(&role),
                content,
                tool_call_id,
                tool_calls,
                ts,
            });
        }
        Ok(out)
    }

    async fn append(
        &self,
        session_key: &str,
        message: SessionMessage,
    ) -> Result<(), CorlinmanError> {
        // Compute next seq under an immediate write transaction so two
        // concurrent appends to the same key can't both observe the same
        // `MAX(seq)`, and so we wait for background trim writers instead of
        // failing on SQLite's read-transaction write upgrade path.
        let mut conn = self
            .pool
            .acquire()
            .await
            .map_err(|e| storage("acquire_conn", e))?;
        sqlx::query("BEGIN IMMEDIATE")
            .execute(&mut *conn)
            .await
            .map_err(|e| storage("begin_immediate", e))?;

        let result: Result<(), CorlinmanError> = async {
            let next_seq: i64 = sqlx::query_scalar(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM sessions WHERE session_key = ?1",
            )
            .bind(session_key)
            .fetch_one(&mut *conn)
            .await
            .map_err(|e| storage("next_seq", e))?;

            let ts_str = message
                .ts
                .format(&Rfc3339)
                .map_err(|e| storage("format_ts", e))?;
            let tool_calls_text = message
                .tool_calls
                .as_ref()
                .map(serde_json::to_string)
                .transpose()
                .map_err(|e| storage("serialize_tool_calls", e))?;

            sqlx::query(
                "INSERT INTO sessions(session_key, seq, role, content, tool_call_id, tool_calls_json, ts) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            )
            .bind(session_key)
            .bind(next_seq)
            .bind(message.role.as_str())
            .bind(&message.content)
            .bind(&message.tool_call_id)
            .bind(&tool_calls_text)
            .bind(&ts_str)
            .execute(&mut *conn)
            .await
            .map_err(|e| storage("insert", e))?;

            Ok(())
        }
        .await;

        match result {
            Ok(()) => {
                sqlx::query("COMMIT")
                    .execute(&mut *conn)
                    .await
                    .map_err(|e| storage("commit", e))?;
                Ok(())
            }
            Err(err) => {
                let _ = sqlx::query("ROLLBACK").execute(&mut *conn).await;
                Err(err)
            }
        }
    }

    async fn delete(&self, session_key: &str) -> Result<(), CorlinmanError> {
        sqlx::query("DELETE FROM sessions WHERE session_key = ?1")
            .bind(session_key)
            .execute(&self.pool)
            .await
            .map_err(|e| storage("delete", e))?;
        Ok(())
    }

    async fn trim(&self, session_key: &str, keep_last_n: usize) -> Result<(), CorlinmanError> {
        if keep_last_n == 0 {
            return self.delete(session_key).await;
        }
        // Keep the N highest-seq rows; delete everything with seq strictly below
        // the cutoff. Correct when the session has fewer than N rows (DELETE
        // matches nothing).
        let keep: i64 = keep_last_n as i64;
        sqlx::query(
            "DELETE FROM sessions \
             WHERE session_key = ?1 \
               AND seq < COALESCE( \
                   (SELECT seq FROM sessions WHERE session_key = ?1 \
                    ORDER BY seq DESC LIMIT 1 OFFSET ?2), \
                   -1)",
        )
        .bind(session_key)
        .bind(keep - 1)
        .execute(&self.pool)
        .await
        .map_err(|e| storage("trim", e))?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    async fn fresh_store() -> (SqliteSessionStore, TempDir) {
        let tmp = TempDir::new().expect("tempdir");
        let path = tmp.path().join("sessions.sqlite");
        let store = SqliteSessionStore::open(&path).await.expect("open");
        (store, tmp)
    }

    #[tokio::test]
    async fn open_creates_schema() {
        let (store, _tmp) = fresh_store().await;
        // Querying an empty session must succeed (table exists).
        let rows = store.load("nope").await.unwrap();
        assert!(rows.is_empty());
        // Confirm the secondary index landed.
        let idx: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sessions_key'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert_eq!(idx.as_deref(), Some("idx_sessions_key"));
    }

    /// Phase 4 W1 4-1A: fresh DB lands with `tenant_id` on `sessions`
    /// and the tenant-aware composite index from TENANT_INDEX_SQL is
    /// present.
    #[tokio::test]
    async fn fresh_db_has_phase4_tenant_column_and_index() {
        let (store, _tmp) = fresh_store().await;

        let cnt: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='tenant_id'",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(cnt, 1, "sessions.tenant_id missing on fresh DB");

        let idx: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sessions_tenant_key'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert_eq!(idx.as_deref(), Some("idx_sessions_tenant_key"));
    }

    /// Phase 4 W1 4-1A: a legacy DB without `tenant_id` must converge
    /// when opened through `SqliteSessionStore::open`. Bootstrap a
    /// pre-Phase-4 file directly via `SqliteConnectOptions`, close,
    /// reopen via the production path.
    #[tokio::test]
    async fn open_migrates_legacy_db_to_phase4_tenant_shape() {
        use sqlx::sqlite::SqliteConnectOptions;

        let tmp = TempDir::new().expect("tempdir");
        let path = tmp.path().join("sessions.sqlite");
        let url = format!("sqlite://{}", path.display());

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
                r#"CREATE TABLE sessions (
                    session_key TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT,
                    tool_calls_json TEXT,
                    ts TEXT NOT NULL,
                    PRIMARY KEY (session_key, seq)
                );
                CREATE INDEX idx_sessions_key ON sessions(session_key);
                INSERT INTO sessions(session_key, seq, role, content, ts)
                    VALUES('legacy-s', 0, 'user', 'pre-tenant message', '2026-04-01T00:00:00Z');
                "#,
            )
            .execute(&pool)
            .await
            .unwrap();
            pool.close().await;
        }

        // Production-path open → migration runs.
        let store = SqliteSessionStore::open(&path).await.unwrap();

        let cnt: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='tenant_id'",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(cnt, 1, "open() must add sessions.tenant_id on legacy DB");

        let idx: Option<String> = sqlx::query_scalar(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sessions_tenant_key'",
        )
        .fetch_optional(store.pool())
        .await
        .unwrap();
        assert_eq!(idx.as_deref(), Some("idx_sessions_tenant_key"));

        // Legacy row backfilled to the reserved 'default' value.
        let backfilled: String =
            sqlx::query_scalar("SELECT tenant_id FROM sessions WHERE session_key='legacy-s'")
                .fetch_one(store.pool())
                .await
                .unwrap();
        assert_eq!(backfilled, "default");

        // Idempotent reopen: production path on already-migrated DB is
        // a clean no-op.
        drop(store);
        let _store2 = SqliteSessionStore::open(&path).await.unwrap();
    }

    #[tokio::test]
    async fn append_and_load_preserve_order() {
        let (store, _tmp) = fresh_store().await;
        store
            .append("s1", SessionMessage::user("hello"))
            .await
            .unwrap();
        store
            .append("s1", SessionMessage::assistant("hi there", None))
            .await
            .unwrap();
        store
            .append("s1", SessionMessage::user("how are you"))
            .await
            .unwrap();

        let msgs = store.load("s1").await.unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[0].role, SessionRole::User);
        assert_eq!(msgs[0].content, "hello");
        assert_eq!(msgs[1].role, SessionRole::Assistant);
        assert_eq!(msgs[1].content, "hi there");
        assert_eq!(msgs[2].role, SessionRole::User);
        assert_eq!(msgs[2].content, "how are you");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn append_waits_for_existing_write_lock() {
        let (store, _tmp) = fresh_store().await;
        let mut locked_conn = store.pool().acquire().await.unwrap();
        sqlx::query("BEGIN IMMEDIATE")
            .execute(&mut *locked_conn)
            .await
            .unwrap();

        let writer = store.clone();
        let append = tokio::spawn(async move {
            writer
                .append("s1", SessionMessage::user("waited for lock"))
                .await
        });

        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        sqlx::query("COMMIT")
            .execute(&mut *locked_conn)
            .await
            .unwrap();

        append.await.unwrap().unwrap();
        let msgs = store.load("s1").await.unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].content, "waited for lock");
    }

    #[tokio::test]
    async fn multiple_sessions_are_isolated() {
        let (store, _tmp) = fresh_store().await;
        store
            .append("a", SessionMessage::user("from a"))
            .await
            .unwrap();
        store
            .append("b", SessionMessage::user("from b"))
            .await
            .unwrap();
        store
            .append("a", SessionMessage::assistant("reply to a", None))
            .await
            .unwrap();

        let a = store.load("a").await.unwrap();
        let b = store.load("b").await.unwrap();
        assert_eq!(a.len(), 2);
        assert_eq!(b.len(), 1);
        assert_eq!(a[0].content, "from a");
        assert_eq!(a[1].content, "reply to a");
        assert_eq!(b[0].content, "from b");
    }

    #[tokio::test]
    async fn trim_keeps_last_n_only() {
        let (store, _tmp) = fresh_store().await;
        for i in 0..10 {
            store
                .append("s", SessionMessage::user(format!("m{i}")))
                .await
                .unwrap();
        }
        store.trim("s", 3).await.unwrap();
        let msgs = store.load("s").await.unwrap();
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[0].content, "m7");
        assert_eq!(msgs[1].content, "m8");
        assert_eq!(msgs[2].content, "m9");
    }

    #[tokio::test]
    async fn trim_below_total_is_noop() {
        let (store, _tmp) = fresh_store().await;
        for i in 0..3 {
            store
                .append("s", SessionMessage::user(format!("m{i}")))
                .await
                .unwrap();
        }
        // keep_last_n > total → keep everything.
        store.trim("s", 100).await.unwrap();
        let msgs = store.load("s").await.unwrap();
        assert_eq!(msgs.len(), 3);
    }

    #[tokio::test]
    async fn delete_clears_session() {
        let (store, _tmp) = fresh_store().await;
        store.append("s", SessionMessage::user("x")).await.unwrap();
        store
            .append("s", SessionMessage::assistant("y", None))
            .await
            .unwrap();
        store.delete("s").await.unwrap();
        assert!(store.load("s").await.unwrap().is_empty());
        // Delete on non-existent session is a no-op.
        store.delete("ghost").await.unwrap();
    }

    #[tokio::test]
    async fn assistant_tool_calls_roundtrip_as_json() {
        let (store, _tmp) = fresh_store().await;
        let tc = serde_json::json!([
            {"id": "call_1", "type": "function",
             "function": {"name": "lookup", "arguments": "{\"q\":\"hi\"}"}}
        ]);
        store
            .append(
                "s",
                SessionMessage::assistant("sure, let me check", Some(tc.clone())),
            )
            .await
            .unwrap();
        let msgs = store.load("s").await.unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].tool_calls.as_ref(), Some(&tc));
    }

    #[tokio::test]
    async fn trim_zero_equivalent_to_delete() {
        let (store, _tmp) = fresh_store().await;
        store.append("s", SessionMessage::user("x")).await.unwrap();
        store.trim("s", 0).await.unwrap();
        assert!(store.load("s").await.unwrap().is_empty());
    }
}
