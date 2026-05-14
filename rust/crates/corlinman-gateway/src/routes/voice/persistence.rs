//! Voice session persistence.
//!
//! Iter 6 of D4. Adds the `voice_sessions` table alongside the existing
//! per-tenant `sessions.sqlite` (owned by `corlinman-core`'s
//! `session_sqlite.rs`). The two tables share a physical file but
//! distinct schemas; the [`SqliteVoiceSessionStore`] in this module
//! opens its own pool against the same path and runs an idempotent
//! `CREATE TABLE IF NOT EXISTS` migration so existing sessions DBs
//! pick up the table on first voice connect.
//!
//! The transcript is **also** written back to the chat `sessions`
//! table — design promise that "the agent loop reads voice turns
//! indistinguishably from typed turns". iter 6 ships the trait surface
//! for that bridge ([`VoiceTranscriptSink`]) and a default
//! [`MemoryTranscriptSink`] for tests; production wiring (the actual
//! `SessionStore::append` call) lives behind a downcast in iter 7+
//! when the agent-loop integration lands. The persistence shape is
//! pinned now so the iter-7 wiring is a one-line state change.
//!
//! ## Audio retention
//!
//! Default `[voice] retain_audio = false` means audio is dropped at
//! session end and `voice_sessions.audio_path` is NULL. When
//! `retain_audio = true`, the gateway writes raw PCM-16 to
//! `<data_dir>/tenants/<t>/voice/<session_id>.pcm`. iter 6 ships the
//! path-resolution helper ([`audio_path_for`]) and the row-write that
//! records the path; the actual audio writes are the bridge tasks'
//! responsibility (iter 9 ties them in).
//!
//! ## End reasons
//!
//! `voice_sessions.end_reason` is a closed set of strings:
//!
//! - `graceful`         — client sent `end` or upstream emitted End{Graceful}
//! - `budget`           — mid-session day-budget cap (close 4002)
//! - `max_session`      — per-session length cap (close 4001)
//! - `provider_error`   — upstream WS dropped or sent an error event
//! - `client_disconnect`— client closed without `end`
//! - `start_failed`     — upstream refused at handshake (no row written
//!   when this happens before the row is inserted; included in the
//!   enum for handlers that catch on a partial write)
//!
//! ## Why not extend `corlinman-core::session_sqlite`?
//!
//! D4's hard-constraint scope is `routes/voice/` plus the `[voice]`
//! config. Touching `session_sqlite.rs` would cross into core territory
//! reserved for a follow-on consolidation iter. Two pools against the
//! same file are SQLite-safe (WAL mode + busy_timeout), and the schema
//! migration is idempotent so concurrent gateway restarts converge.

use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;

use async_trait::async_trait;
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Row, SqlitePool};

/// Schema applied on first open. Idempotent via `IF NOT EXISTS`.
///
/// Keeping the index here matches the design's
/// `idx_voice_sessions_tenant_session` shape — the admin "list voice
/// sessions for tenant T since timestamp" query is the hot path.
pub const VOICE_SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS voice_sessions (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    session_key     TEXT NOT NULL,
    agent_id        TEXT,
    provider_alias  TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    duration_secs   INTEGER,
    audio_path      TEXT,
    transcript_text TEXT,
    end_reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_tenant_session
    ON voice_sessions(tenant_id, session_key, started_at);
"#;

/// Closed set of session-end reasons. The `as_str()` form is what
/// lands in the `end_reason` column; spelt out so a casual operator
/// query like `SELECT end_reason, COUNT(*) FROM voice_sessions GROUP
/// BY end_reason` shows readable buckets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceEndReason {
    Graceful,
    Budget,
    MaxSession,
    ProviderError,
    ClientDisconnect,
    StartFailed,
}

impl VoiceEndReason {
    pub fn as_str(self) -> &'static str {
        match self {
            VoiceEndReason::Graceful => "graceful",
            VoiceEndReason::Budget => "budget",
            VoiceEndReason::MaxSession => "max_session",
            VoiceEndReason::ProviderError => "provider_error",
            VoiceEndReason::ClientDisconnect => "client_disconnect",
            VoiceEndReason::StartFailed => "start_failed",
        }
    }
}

/// Insert-time payload — fields known when the session opens (before
/// any audio flows). The row is updated in-place on session end with
/// the duration / transcript / end_reason columns.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VoiceSessionStart {
    pub id: String,
    pub tenant_id: String,
    pub session_key: String,
    pub agent_id: Option<String>,
    pub provider_alias: String,
    /// Unix seconds. The route handler stamps this; tests inject a
    /// fixed clock through this field.
    pub started_at: i64,
}

/// Update-time payload — fields known at session close. The route
/// handler builds this from the [`super::cost::SessionMeter`] and the
/// accumulated transcript.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VoiceSessionEnd {
    pub id: String,
    pub ended_at: i64,
    pub duration_secs: i64,
    pub audio_path: Option<String>,
    pub transcript_text: Option<String>,
    pub end_reason: VoiceEndReason,
}

/// Read shape — used by the iter-6 tests and (later) the admin UI's
/// voice-session-history view. Keeps the column → field mapping in
/// one place.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VoiceSessionRow {
    pub id: String,
    pub tenant_id: String,
    pub session_key: String,
    pub agent_id: Option<String>,
    pub provider_alias: String,
    pub started_at: i64,
    pub ended_at: Option<i64>,
    pub duration_secs: Option<i64>,
    pub audio_path: Option<String>,
    pub transcript_text: Option<String>,
    pub end_reason: String,
}

/// Trait surface so iter 6 tests can drive a pure in-memory store and
/// production uses the SQLite-backed [`SqliteVoiceSessionStore`].
#[async_trait]
pub trait VoiceSessionStore: Send + Sync {
    /// Insert a row at session start with `end_reason = "graceful"` as
    /// a placeholder; finalisation overwrites it on close.
    async fn record_start(&self, start: &VoiceSessionStart) -> Result<(), VoiceStoreError>;

    /// Update the row written by [`record_start`]. If the id doesn't
    /// match a row, returns [`VoiceStoreError::RowMissing`] — defends
    /// against double-finalisation.
    async fn record_end(&self, end: &VoiceSessionEnd) -> Result<(), VoiceStoreError>;

    /// Load a single row (tests + admin UI).
    async fn fetch(&self, id: &str) -> Result<Option<VoiceSessionRow>, VoiceStoreError>;

    /// List rows for a tenant + session_key, most-recent first. Used
    /// by the chat view to surface "this conversation also has voice
    /// turns" badges.
    async fn list_for_session(
        &self,
        tenant_id: &str,
        session_key: &str,
    ) -> Result<Vec<VoiceSessionRow>, VoiceStoreError>;
}

#[derive(Debug, thiserror::Error)]
pub enum VoiceStoreError {
    #[error("voice store SQL error: {detail}")]
    Sql { detail: String },
    #[error("voice store row missing: {id}")]
    RowMissing { id: String },
}

fn sql<E: std::fmt::Display>(e: E) -> VoiceStoreError {
    VoiceStoreError::Sql {
        detail: e.to_string(),
    }
}

/// SQLite-backed voice session store. Cheap to clone; internally
/// holds a pooled connection.
#[derive(Debug, Clone)]
pub struct SqliteVoiceSessionStore {
    pool: SqlitePool,
}

impl SqliteVoiceSessionStore {
    /// Open (or create) the voice-sessions DB at `path`. The path is
    /// **the same file** as the chat `sessions.sqlite` (per design),
    /// but a separate pool so this module never reaches into core.
    ///
    /// Uses WAL + `synchronous=NORMAL` for write throughput, matching
    /// the core's pool settings; concurrent access from both pools is
    /// SQLite-safe under WAL.
    pub async fn open(path: &Path) -> Result<Self, VoiceStoreError> {
        let url = format!("sqlite://{}", path.display());
        let options = SqliteConnectOptions::from_str(&url)
            .map_err(sql)?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .busy_timeout(std::time::Duration::from_secs(5));
        let pool = SqlitePoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .map_err(sql)?;
        sqlx::raw_sql(VOICE_SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(sql)?;
        Ok(Self { pool })
    }

    /// Test seam — bypass the file path entirely.
    #[cfg(test)]
    pub async fn open_in_memory() -> Result<Self, VoiceStoreError> {
        let options = SqliteConnectOptions::from_str("sqlite::memory:")
            .map_err(sql)?
            .create_if_missing(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(1) // shared in-memory: keep a single conn
            .connect_with(options)
            .await
            .map_err(sql)?;
        sqlx::raw_sql(VOICE_SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(sql)?;
        Ok(Self { pool })
    }
}

#[async_trait]
impl VoiceSessionStore for SqliteVoiceSessionStore {
    async fn record_start(&self, start: &VoiceSessionStart) -> Result<(), VoiceStoreError> {
        sqlx::query(
            "INSERT INTO voice_sessions \
             (id, tenant_id, session_key, agent_id, provider_alias, \
              started_at, ended_at, duration_secs, audio_path, \
              transcript_text, end_reason) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL, NULL, ?7)",
        )
        .bind(&start.id)
        .bind(&start.tenant_id)
        .bind(&start.session_key)
        .bind(&start.agent_id)
        .bind(&start.provider_alias)
        .bind(start.started_at)
        // Placeholder; overwritten by record_end. Using "graceful" as
        // the default so a row that's never finalised (gateway crash)
        // still has a valid end_reason. iter 8+ will add a sweeper
        // job that flips never-ended rows to `client_disconnect`.
        .bind(VoiceEndReason::Graceful.as_str())
        .execute(&self.pool)
        .await
        .map_err(sql)?;
        Ok(())
    }

    async fn record_end(&self, end: &VoiceSessionEnd) -> Result<(), VoiceStoreError> {
        let res = sqlx::query(
            "UPDATE voice_sessions SET \
                ended_at = ?1, \
                duration_secs = ?2, \
                audio_path = ?3, \
                transcript_text = ?4, \
                end_reason = ?5 \
             WHERE id = ?6",
        )
        .bind(end.ended_at)
        .bind(end.duration_secs)
        .bind(&end.audio_path)
        .bind(&end.transcript_text)
        .bind(end.end_reason.as_str())
        .bind(&end.id)
        .execute(&self.pool)
        .await
        .map_err(sql)?;
        if res.rows_affected() == 0 {
            return Err(VoiceStoreError::RowMissing { id: end.id.clone() });
        }
        Ok(())
    }

    async fn fetch(&self, id: &str) -> Result<Option<VoiceSessionRow>, VoiceStoreError> {
        let row = sqlx::query(
            "SELECT id, tenant_id, session_key, agent_id, provider_alias, \
                    started_at, ended_at, duration_secs, audio_path, \
                    transcript_text, end_reason \
             FROM voice_sessions WHERE id = ?1",
        )
        .bind(id)
        .fetch_optional(&self.pool)
        .await
        .map_err(sql)?;
        Ok(row.map(row_to_voice))
    }

    async fn list_for_session(
        &self,
        tenant_id: &str,
        session_key: &str,
    ) -> Result<Vec<VoiceSessionRow>, VoiceStoreError> {
        let rows = sqlx::query(
            "SELECT id, tenant_id, session_key, agent_id, provider_alias, \
                    started_at, ended_at, duration_secs, audio_path, \
                    transcript_text, end_reason \
             FROM voice_sessions \
             WHERE tenant_id = ?1 AND session_key = ?2 \
             ORDER BY started_at DESC",
        )
        .bind(tenant_id)
        .bind(session_key)
        .fetch_all(&self.pool)
        .await
        .map_err(sql)?;
        Ok(rows.into_iter().map(row_to_voice).collect())
    }
}

fn row_to_voice(r: sqlx::sqlite::SqliteRow) -> VoiceSessionRow {
    // sqlx's `try_get::<T, _>` on a NULL column returns `Err` for some
    // primitive `T` and a zeroed `Ok` for others, depending on the
    // column type — `.ok()` then loses the NULL distinction. Reading
    // `Option<T>` explicitly preserves NULL → None unambiguously.
    let agent_id: Option<String> = r.try_get("agent_id").unwrap_or(None);
    let ended_at: Option<i64> = r.try_get("ended_at").unwrap_or(None);
    let duration_secs: Option<i64> = r.try_get("duration_secs").unwrap_or(None);
    let audio_path: Option<String> = r.try_get("audio_path").unwrap_or(None);
    let transcript_text: Option<String> = r.try_get("transcript_text").unwrap_or(None);
    VoiceSessionRow {
        id: r.get("id"),
        tenant_id: r.get("tenant_id"),
        session_key: r.get("session_key"),
        agent_id,
        provider_alias: r.get("provider_alias"),
        started_at: r.get("started_at"),
        ended_at,
        duration_secs,
        audio_path,
        transcript_text,
        end_reason: r.get("end_reason"),
    }
}

// ---------------------------------------------------------------------------
// Audio retention path resolution
// ---------------------------------------------------------------------------

/// Resolve the on-disk PCM path for one voice session under
/// `<data_dir>/tenants/<t>/voice/<session_id>.pcm`.
///
/// Pure: doesn't create the file or directory. The caller (iter 9's
/// retention-sweeper writer) is responsible for `mkdir -p` and the
/// per-session file handle. Returning a `PathBuf` keeps the path
/// composition logic testable without filesystem access.
pub fn audio_path_for(data_dir: &Path, tenant_id: &str, session_id: &str) -> PathBuf {
    data_dir
        .join("tenants")
        .join(tenant_id)
        .join("voice")
        .join(format!("{session_id}.pcm"))
}

/// TTS sibling path for retained assistant audio. Lives next to the
/// inbound PCM under the same per-tenant tree so the retention sweeper
/// can match both with one glob.
pub fn tts_audio_path_for(data_dir: &Path, tenant_id: &str, session_id: &str) -> PathBuf {
    data_dir
        .join("tenants")
        .join(tenant_id)
        .join("voice")
        .join(format!("{session_id}.tts.pcm"))
}

// ---------------------------------------------------------------------------
// Transcript bridge — voice turns → chat sessions table
// ---------------------------------------------------------------------------

/// Trait so iter 6 can wire a route-handler-side bridge (a real
/// `SessionStore` adapter) without touching `corlinman-core`. iter 7+
/// constructs a [`SessionStoreTranscriptSink`] (private adapter) over
/// the existing core store; iter 6 ships the trait + a memory impl.
#[async_trait]
pub trait VoiceTranscriptSink: Send + Sync {
    /// Append one voice turn to the chat session under the given
    /// `(tenant_id, session_key)`. `role` is `"user"` or `"assistant"`;
    /// `text` is the committed transcript line.
    async fn append_turn(
        &self,
        tenant_id: &str,
        session_key: &str,
        role: &str,
        text: &str,
    ) -> Result<(), VoiceStoreError>;
}

/// In-memory implementation for tests + a default no-op deployment
/// path while the iter-7 wiring lands. Captures appended turns in a
/// mutex-guarded vec so tests can assert ordering / content.
#[derive(Debug, Default)]
pub struct MemoryTranscriptSink {
    inner: tokio::sync::Mutex<Vec<TranscriptedTurn>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptedTurn {
    pub tenant_id: String,
    pub session_key: String,
    pub role: String,
    pub text: String,
}

impl MemoryTranscriptSink {
    pub fn new() -> Self {
        Self::default()
    }

    /// Snapshot the appended turns. Cloned out so the caller doesn't
    /// hold a lock across awaits.
    pub async fn snapshot(&self) -> Vec<TranscriptedTurn> {
        self.inner.lock().await.clone()
    }
}

#[async_trait]
impl VoiceTranscriptSink for MemoryTranscriptSink {
    async fn append_turn(
        &self,
        tenant_id: &str,
        session_key: &str,
        role: &str,
        text: &str,
    ) -> Result<(), VoiceStoreError> {
        self.inner.lock().await.push(TranscriptedTurn {
            tenant_id: tenant_id.to_string(),
            session_key: session_key.to_string(),
            role: role.to_string(),
            text: text.to_string(),
        });
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Convenience type aliases for the route handler's state
// ---------------------------------------------------------------------------

/// Shared handle the route handler stores in `VoiceState` (iter 7+).
pub type SharedVoiceSessionStore = Arc<dyn VoiceSessionStore>;

/// Shared transcript sink for the chat-session bridge.
pub type SharedTranscriptSink = Arc<dyn VoiceTranscriptSink>;

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn start_row(id: &str) -> VoiceSessionStart {
        VoiceSessionStart {
            id: id.into(),
            tenant_id: "tenant-a".into(),
            session_key: "sess-1".into(),
            agent_id: Some("agent-1".into()),
            provider_alias: "openai-realtime".into(),
            started_at: 1_700_000_000,
        }
    }

    fn end_row(id: &str, audio_path: Option<&str>, transcript: Option<&str>) -> VoiceSessionEnd {
        VoiceSessionEnd {
            id: id.into(),
            ended_at: 1_700_000_120,
            duration_secs: 120,
            audio_path: audio_path.map(str::to_string),
            transcript_text: transcript.map(str::to_string),
            end_reason: VoiceEndReason::Graceful,
        }
    }

    // ----- VoiceEndReason -----

    #[test]
    fn end_reason_strings_are_stable() {
        // Pinned because the strings are persisted; renaming a variant
        // without updating the column would create unreadable rows in
        // older DBs. Acts as a regression alarm.
        assert_eq!(VoiceEndReason::Graceful.as_str(), "graceful");
        assert_eq!(VoiceEndReason::Budget.as_str(), "budget");
        assert_eq!(VoiceEndReason::MaxSession.as_str(), "max_session");
        assert_eq!(VoiceEndReason::ProviderError.as_str(), "provider_error");
        assert_eq!(
            VoiceEndReason::ClientDisconnect.as_str(),
            "client_disconnect"
        );
        assert_eq!(VoiceEndReason::StartFailed.as_str(), "start_failed");
    }

    // ----- audio path resolution -----

    #[test]
    fn audio_path_for_resolves_under_tenant_tree() {
        let p = audio_path_for(Path::new("/data"), "t-1", "voice-abc");
        assert_eq!(p.to_string_lossy(), "/data/tenants/t-1/voice/voice-abc.pcm");
    }

    #[test]
    fn tts_audio_path_for_resolves_with_tts_suffix() {
        let p = tts_audio_path_for(Path::new("/data"), "t-1", "voice-abc");
        assert_eq!(
            p.to_string_lossy(),
            "/data/tenants/t-1/voice/voice-abc.tts.pcm"
        );
    }

    // ----- SqliteVoiceSessionStore -----

    #[tokio::test]
    async fn schema_applies_idempotently_on_reopen() {
        // Open twice in-memory — the second open must not error on
        // CREATE TABLE because of IF NOT EXISTS. (The in-memory pool
        // doesn't share state across open() calls; the test exercises
        // the schema-application step itself.)
        let s1 = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        drop(s1);
        let _s2 = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
    }

    #[tokio::test]
    async fn record_start_then_fetch_round_trips_basic_fields() {
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        s.record_start(&start_row("voice-1")).await.unwrap();
        let row = s.fetch("voice-1").await.unwrap().expect("row present");
        assert_eq!(row.id, "voice-1");
        assert_eq!(row.tenant_id, "tenant-a");
        assert_eq!(row.session_key, "sess-1");
        assert_eq!(row.agent_id.as_deref(), Some("agent-1"));
        assert_eq!(row.provider_alias, "openai-realtime");
        assert_eq!(row.started_at, 1_700_000_000);
        assert!(row.ended_at.is_none());
        assert!(row.duration_secs.is_none());
        assert!(row.audio_path.is_none());
        assert!(row.transcript_text.is_none());
        assert_eq!(row.end_reason, "graceful");
    }

    #[tokio::test]
    async fn record_end_finalises_row() {
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        s.record_start(&start_row("voice-2")).await.unwrap();
        s.record_end(&end_row(
            "voice-2",
            Some("/data/tenants/tenant-a/voice/voice-2.pcm"),
            Some("user: hi\nassistant: hello"),
        ))
        .await
        .unwrap();
        let row = s.fetch("voice-2").await.unwrap().unwrap();
        assert_eq!(row.ended_at, Some(1_700_000_120));
        assert_eq!(row.duration_secs, Some(120));
        assert_eq!(
            row.audio_path.as_deref(),
            Some("/data/tenants/tenant-a/voice/voice-2.pcm")
        );
        assert_eq!(
            row.transcript_text.as_deref(),
            Some("user: hi\nassistant: hello")
        );
        assert_eq!(row.end_reason, "graceful");
    }

    #[tokio::test]
    async fn record_end_for_unknown_id_errors() {
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        let err = s
            .record_end(&end_row("nonexistent", None, None))
            .await
            .unwrap_err();
        assert!(matches!(err, VoiceStoreError::RowMissing { id } if id == "nonexistent"));
    }

    #[tokio::test]
    async fn record_end_with_retain_off_persists_null_audio_path() {
        // `retain_audio = false` path: the route handler builds an
        // end-row with `audio_path = None`. Pinned so a future
        // refactor doesn't accidentally substitute an empty string.
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        s.record_start(&start_row("voice-3")).await.unwrap();
        s.record_end(&VoiceSessionEnd {
            id: "voice-3".into(),
            ended_at: 1,
            duration_secs: 1,
            audio_path: None,
            transcript_text: Some("transcript".into()),
            end_reason: VoiceEndReason::Graceful,
        })
        .await
        .unwrap();
        let row = s.fetch("voice-3").await.unwrap().unwrap();
        assert!(
            row.audio_path.is_none(),
            "audio_path must be NULL not empty"
        );
        assert_eq!(row.transcript_text.as_deref(), Some("transcript"));
    }

    #[tokio::test]
    async fn list_for_session_returns_rows_most_recent_first() {
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        let mut a = start_row("a");
        a.started_at = 1_700_000_000;
        let mut b = start_row("b");
        b.started_at = 1_700_000_500;
        let mut c = start_row("c");
        c.started_at = 1_700_001_000;
        s.record_start(&a).await.unwrap();
        s.record_start(&b).await.unwrap();
        s.record_start(&c).await.unwrap();

        // A tenant-isolation row that must NOT show up.
        let mut other = start_row("other-tenant");
        other.tenant_id = "tenant-b".into();
        s.record_start(&other).await.unwrap();

        // A different session-key row that must NOT show up.
        let mut other_session = start_row("other-session");
        other_session.session_key = "sess-2".into();
        s.record_start(&other_session).await.unwrap();

        let rows = s.list_for_session("tenant-a", "sess-1").await.unwrap();
        assert_eq!(rows.len(), 3, "got: {:?}", rows);
        assert_eq!(rows[0].id, "c");
        assert_eq!(rows[1].id, "b");
        assert_eq!(rows[2].id, "a");
    }

    #[tokio::test]
    async fn list_for_session_returns_empty_when_no_match() {
        let s = SqliteVoiceSessionStore::open_in_memory().await.unwrap();
        let rows = s.list_for_session("ghost", "ghost").await.unwrap();
        assert!(rows.is_empty());
    }

    // ----- in-memory transcript sink -----

    #[tokio::test]
    async fn memory_sink_captures_appended_turns_in_order() {
        let sink = MemoryTranscriptSink::new();
        sink.append_turn("t", "k", "user", "hi").await.unwrap();
        sink.append_turn("t", "k", "assistant", "hello")
            .await
            .unwrap();
        let snap = sink.snapshot().await;
        assert_eq!(snap.len(), 2);
        assert_eq!(snap[0].role, "user");
        assert_eq!(snap[0].text, "hi");
        assert_eq!(snap[1].role, "assistant");
        assert_eq!(snap[1].text, "hello");
    }

    #[tokio::test]
    async fn memory_sink_preserves_session_isolation() {
        let sink = MemoryTranscriptSink::new();
        sink.append_turn("t1", "k1", "user", "a").await.unwrap();
        sink.append_turn("t2", "k1", "user", "b").await.unwrap();
        sink.append_turn("t1", "k2", "user", "c").await.unwrap();
        let snap = sink.snapshot().await;
        assert_eq!(snap.len(), 3);
        // Stored as-is — the route handler queries by (tenant, key)
        // when reading; the sink itself just appends in order so the
        // iter-7 wiring can flush in submission order.
        assert_eq!(snap[0].tenant_id, "t1");
        assert_eq!(snap[0].session_key, "k1");
        assert_eq!(snap[1].tenant_id, "t2");
        assert_eq!(snap[2].session_key, "k2");
    }

    // ----- file-backed open() smoke -----

    #[tokio::test]
    async fn open_creates_db_file_when_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("sessions.sqlite");
        assert!(!path.exists());
        let s = SqliteVoiceSessionStore::open(&path).await.unwrap();
        assert!(path.exists(), "file must be created on open()");

        // Round-trip a row through the file-backed pool.
        s.record_start(&start_row("voice-file")).await.unwrap();
        let row = s.fetch("voice-file").await.unwrap().unwrap();
        assert_eq!(row.id, "voice-file");
    }
}
