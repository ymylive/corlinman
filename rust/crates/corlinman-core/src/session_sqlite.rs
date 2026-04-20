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
use crate::session::{SessionMessage, SessionRole, SessionStore};

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
    PRIMARY KEY (session_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);
"#;

/// Storage error helper — wrap a sqlx error into `CorlinmanError::Storage` with
/// a short operation tag so logs can distinguish failing queries.
fn storage<E: std::fmt::Display>(op: &str, e: E) -> CorlinmanError {
    CorlinmanError::Storage(format!("sessions {op}: {e}"))
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
            .synchronous(SqliteSynchronous::Normal);

        let pool = SqlitePoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .map_err(|e| storage("connect", e))?;

        sqlx::raw_sql(SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(|e| storage("apply_schema", e))?;

        Ok(Self { pool })
    }

    /// Borrow the pool (tests only).
    #[cfg(test)]
    pub(crate) fn pool(&self) -> &SqlitePool {
        &self.pool
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
        // Compute next seq under a transaction so two concurrent appends to the
        // same key can't both observe the same `MAX(seq)`.
        let mut tx = self
            .pool
            .begin()
            .await
            .map_err(|e| storage("begin_tx", e))?;

        let next_seq: i64 = sqlx::query_scalar(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM sessions WHERE session_key = ?1",
        )
        .bind(session_key)
        .fetch_one(&mut *tx)
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
        .execute(&mut *tx)
        .await
        .map_err(|e| storage("insert", e))?;

        tx.commit().await.map_err(|e| storage("commit", e))?;
        Ok(())
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
