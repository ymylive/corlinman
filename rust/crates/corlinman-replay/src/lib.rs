//! Phase 4 Wave 2 4-2D — deterministic session replay primitive.
//!
//! Loads a session by key from `sessions.sqlite` and reconstructs a
//! structured transcript that downstream callers (the `corlinman
//! replay` CLI; the `/admin/sessions/:key/replay` HTTP route) format
//! for human or JSON consumption.
//!
//! # Modes
//!
//! - **Transcript** (default): read-only deterministic dump of the
//!   stored session messages, ordered by `seq` ASC. No agent
//!   execution. Idempotent: same `(sessions.sqlite, session_key)`
//!   always yields the same transcript.
//!
//! - **Rerun** (Wave 2.5+, stub in v1): would re-feed user-role
//!   messages back through the agent client and capture the new
//!   assistant outputs alongside the originals. v1 ships the wire
//!   shape with a `not_implemented_yet` marker so the UI can render
//!   the deferral; the actual diff renderer ships in Wave 2.5.
//!
//! # Tenant scoping
//!
//! The crate is tenant-aware: callers pass a [`corlinman_tenant::TenantId`]
//! and the replay primitive opens
//! `<data_dir>/tenants/<tenant>/sessions.sqlite`. Single-tenant
//! deployments pass `TenantId::legacy_default()` and read from the
//! reserved-default path; this matches the gateway's per-tenant
//! file layout that Phase 4 W1 4-1A landed.

use std::path::{Path, PathBuf};

use corlinman_core::{SessionRole, SessionStore, SessionSummary, SqliteSessionStore};
use corlinman_tenant::{tenant_db_path, TenantId};
use serde::{Deserialize, Serialize};
use time::format_description::well_known::Rfc3339;

/// Replay execution mode. See module docs for the deferral note on
/// `Rerun`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ReplayMode {
    #[default]
    Transcript,
    Rerun,
}

impl ReplayMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Transcript => "transcript",
            Self::Rerun => "rerun",
        }
    }
}

/// One row in the replay transcript. Mirrors
/// [`corlinman_core::SessionMessage`] but with the timestamp pinned
/// to RFC-3339 (UI consumption) and the role serialised as a
/// lowercase string to match the JSON wire shape.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReplayMessage {
    pub role: String,
    pub content: String,
    /// RFC-3339 / ISO-8601 string. Matches the
    /// `tenants.created_at` and `evolution_history.applied_at`
    /// formatting conventions used elsewhere in the admin surface.
    pub ts: String,
}

/// Summary block in the replay output. Carries metadata the UI
/// needs to render headers without re-querying.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplaySummary {
    pub message_count: usize,
    pub tenant_id: String,
    /// Wave 2.5 deferral marker. Set to `Some("not_implemented_yet")`
    /// in v1 when [`ReplayMode::Rerun`] is requested. `None` for
    /// transcript mode and once rerun ships.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rerun_diff: Option<String>,
}

/// Top-level replay output. Direct serde shape for the
/// `/admin/sessions/:key/replay` HTTP route and `corlinman replay`
/// CLI's `--output json` mode.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplayOutput {
    pub session_key: String,
    pub mode: String,
    pub transcript: Vec<ReplayMessage>,
    pub summary: ReplaySummary,
}

/// One row in the admin sessions list. Mirrors the UI's
/// `SessionSummary` interface in `ui/lib/api/sessions.ts`. Distinct
/// from [`corlinman_core::SessionSummary`] only in the field names —
/// kept separate so the wire shape can evolve without touching the
/// store.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionListRow {
    pub session_key: String,
    /// Unix milliseconds of the most-recent message in the session.
    pub last_message_at: i64,
    pub message_count: i64,
}

impl From<SessionSummary> for SessionListRow {
    fn from(s: SessionSummary) -> Self {
        Self {
            session_key: s.session_key,
            last_message_at: s.last_message_at_ms,
            message_count: s.message_count,
        }
    }
}

/// Errors emitted by [`replay`].
#[derive(Debug, thiserror::Error)]
pub enum ReplayError {
    #[error("session store open failed at {path}: {source}")]
    StoreOpen {
        path: PathBuf,
        #[source]
        source: corlinman_core::CorlinmanError,
    },
    #[error("session store load failed for key {key:?}: {source}")]
    StoreLoad {
        key: String,
        #[source]
        source: corlinman_core::CorlinmanError,
    },
    #[error("session not found: {0:?}")]
    SessionNotFound(String),
}

/// List all sessions stored under `<data_dir>/tenants/<tenant>/sessions.sqlite`.
///
/// Returns an empty vec when the file exists but holds no sessions.
/// `ReplayError::StoreOpen` propagates if the file can't be opened
/// (e.g. tenant dir missing — caller decides whether to treat as
/// empty list or 503).
pub async fn list_sessions(
    data_dir: &Path,
    tenant: &TenantId,
) -> Result<Vec<SessionListRow>, ReplayError> {
    let path = sessions_db_path(data_dir, tenant);
    let store =
        SqliteSessionStore::open(&path)
            .await
            .map_err(|source| ReplayError::StoreOpen {
                path: path.clone(),
                source,
            })?;

    let rows = store
        .list_sessions()
        .await
        .map_err(|source| ReplayError::StoreLoad {
            key: "<list>".into(),
            source,
        })?;

    Ok(rows.into_iter().map(SessionListRow::from).collect())
}

/// Resolve the per-tenant `sessions.sqlite` path under `data_dir`
/// using the same convention the gateway uses
/// (`<data_dir>/tenants/<tenant>/sessions.sqlite`). When the tenant
/// is `default` this collapses to the legacy single-tenant path
/// segment.
pub fn sessions_db_path(data_dir: &Path, tenant: &TenantId) -> PathBuf {
    tenant_db_path(data_dir, tenant, "sessions")
}

/// Load a session and reconstruct the deterministic replay output.
///
/// Returns [`ReplayError::SessionNotFound`] when the key has no
/// stored messages — distinguishes "session was pruned / never
/// existed" from "session exists but is empty"; the latter case
/// returns `Ok` with `transcript: vec![]`. v1 treats both as 404
/// at the HTTP layer.
pub async fn replay(
    data_dir: &Path,
    tenant: &TenantId,
    session_key: &str,
    mode: ReplayMode,
) -> Result<ReplayOutput, ReplayError> {
    let path = sessions_db_path(data_dir, tenant);
    let store = SqliteSessionStore::open(&path)
        .await
        .map_err(|source| ReplayError::StoreOpen {
            path: path.clone(),
            source,
        })?;

    let messages =
        store
            .load(session_key)
            .await
            .map_err(|source| ReplayError::StoreLoad {
                key: session_key.to_string(),
                source,
            })?;

    if messages.is_empty() {
        return Err(ReplayError::SessionNotFound(session_key.to_string()));
    }

    let transcript: Vec<ReplayMessage> = messages
        .iter()
        .map(|m| ReplayMessage {
            role: format!("{}", role_str(m.role)),
            content: m.content.clone(),
            ts: m.ts.format(&Rfc3339).unwrap_or_default(),
        })
        .collect();

    let summary = ReplaySummary {
        message_count: transcript.len(),
        tenant_id: tenant.as_str().to_string(),
        rerun_diff: match mode {
            ReplayMode::Rerun => Some("not_implemented_yet".to_string()),
            ReplayMode::Transcript => None,
        },
    };

    Ok(ReplayOutput {
        session_key: session_key.to_string(),
        mode: mode.as_str().to_string(),
        transcript,
        summary,
    })
}

fn role_str(role: SessionRole) -> &'static str {
    match role {
        SessionRole::User => "user",
        SessionRole::Assistant => "assistant",
        SessionRole::System => "system",
        SessionRole::Tool => "tool",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::SessionMessage;
    use tempfile::TempDir;

    /// Open a fresh `sessions.sqlite` under a tempdir + tenant dir,
    /// seed three messages, and return the tempdir so callers can
    /// drive the replay against the same data dir root.
    async fn seed(messages: Vec<SessionMessage>) -> (TempDir, TenantId) {
        let tmp = TempDir::new().unwrap();
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        for m in messages {
            store.append("test-session", m).await.unwrap();
        }
        (tmp, tenant)
    }

    fn msg(role: SessionRole, content: &str) -> SessionMessage {
        SessionMessage {
            role,
            content: content.to_string(),
            tool_call_id: None,
            tool_calls: None,
            ts: time::OffsetDateTime::UNIX_EPOCH + time::Duration::seconds(1_777_593_600),
        }
    }

    #[tokio::test]
    async fn transcript_mode_returns_messages_in_seq_order() {
        let (tmp, tenant) = seed(vec![
            msg(SessionRole::User, "hello"),
            msg(SessionRole::Assistant, "hi there"),
            msg(SessionRole::User, "how are you"),
        ])
        .await;

        let out = replay(tmp.path(), &tenant, "test-session", ReplayMode::Transcript)
            .await
            .unwrap();

        assert_eq!(out.session_key, "test-session");
        assert_eq!(out.mode, "transcript");
        assert_eq!(out.transcript.len(), 3);
        assert_eq!(out.transcript[0].role, "user");
        assert_eq!(out.transcript[0].content, "hello");
        assert_eq!(out.transcript[1].role, "assistant");
        assert_eq!(out.transcript[2].content, "how are you");
        // RFC-3339 round-trip — must produce a parseable timestamp.
        assert!(out.transcript[0].ts.starts_with("2026-"));
        assert_eq!(out.summary.message_count, 3);
        assert_eq!(out.summary.tenant_id, "default");
        assert!(out.summary.rerun_diff.is_none());
    }

    #[tokio::test]
    async fn rerun_mode_emits_not_implemented_marker() {
        let (tmp, tenant) = seed(vec![msg(SessionRole::User, "ping")]).await;

        let out = replay(tmp.path(), &tenant, "test-session", ReplayMode::Rerun)
            .await
            .unwrap();

        assert_eq!(out.mode, "rerun");
        assert_eq!(out.transcript.len(), 1);
        assert_eq!(
            out.summary.rerun_diff.as_deref(),
            Some("not_implemented_yet"),
            "v1 ships the wire shape with a placeholder; Wave 2.5 \
             swaps in the diff renderer"
        );
    }

    #[tokio::test]
    async fn missing_session_returns_session_not_found() {
        let tmp = TempDir::new().unwrap();
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        // Open the store so the SQLite file exists but no messages
        // are seeded.
        let _store = SqliteSessionStore::open(&path).await.unwrap();

        let err = replay(
            tmp.path(),
            &tenant,
            "ghost-session",
            ReplayMode::Transcript,
        )
        .await
        .expect_err("missing session must error");

        assert!(matches!(err, ReplayError::SessionNotFound(k) if k == "ghost-session"));
    }

    #[tokio::test]
    async fn list_sessions_groups_by_key_ordered_by_last_ts_desc() {
        let tmp = TempDir::new().unwrap();
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();

        // Older session: two messages.
        let older = SessionMessage {
            ts: time::OffsetDateTime::UNIX_EPOCH + time::Duration::seconds(1_700_000_000),
            ..msg(SessionRole::User, "old-1")
        };
        let older2 = SessionMessage {
            ts: time::OffsetDateTime::UNIX_EPOCH + time::Duration::seconds(1_700_000_001),
            ..msg(SessionRole::Assistant, "old-2")
        };
        store.append("session-old", older).await.unwrap();
        store.append("session-old", older2).await.unwrap();

        // Newer session: one message.
        let newer = SessionMessage {
            ts: time::OffsetDateTime::UNIX_EPOCH + time::Duration::seconds(1_800_000_000),
            ..msg(SessionRole::User, "new-1")
        };
        store.append("session-new", newer).await.unwrap();

        let rows = list_sessions(tmp.path(), &tenant).await.unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].session_key, "session-new", "newest first");
        assert_eq!(rows[0].message_count, 1);
        assert_eq!(rows[1].session_key, "session-old");
        assert_eq!(rows[1].message_count, 2);
        // Unix-ms conversion sanity: 1_800_000_000s == 1_800_000_000_000ms.
        assert_eq!(rows[0].last_message_at, 1_800_000_000_000);
    }

    #[tokio::test]
    async fn list_sessions_empty_when_no_messages() {
        let tmp = TempDir::new().unwrap();
        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let _ = SqliteSessionStore::open(&path).await.unwrap();

        let rows = list_sessions(tmp.path(), &tenant).await.unwrap();
        assert!(rows.is_empty());
    }

    #[tokio::test]
    async fn non_default_tenant_routes_to_per_tenant_path() {
        let acme = TenantId::new("acme").unwrap();
        let tmp = TempDir::new().unwrap();
        let path = sessions_db_path(tmp.path(), &acme);
        // Per-tenant path resolution must place the file under
        // `<root>/tenants/acme/sessions.sqlite`.
        assert!(path.to_string_lossy().contains("/tenants/acme/"));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("acme-session", msg(SessionRole::User, "moin"))
            .await
            .unwrap();

        let out = replay(tmp.path(), &acme, "acme-session", ReplayMode::Transcript)
            .await
            .unwrap();
        assert_eq!(out.summary.tenant_id, "acme");
    }
}
