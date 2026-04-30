//! `/admin/sessions*` — operator-facing replay surface (Phase 4 W2 4-2D).
//!
//! Two routes, both behind `require_admin` and `tenant_scope`. They
//! read per-tenant `<data_dir>/tenants/<slug>/sessions.sqlite` via the
//! `corlinman-replay` primitive, so the wire shape is shared with the
//! `corlinman replay --output json` CLI:
//!
//! - `GET  /admin/sessions` — list of sessions with metadata. The
//!   payload mirrors the UI's `SessionSummary` contract in
//!   `ui/lib/api/sessions.ts`. `last_message_at` is unix milliseconds
//!   so the UI can `Date(ms).toLocaleString()` directly.
//! - `POST /admin/sessions/:key/replay` — deterministic transcript
//!   dump. Body is `{ "mode": "transcript" | "rerun" }`; both default
//!   to `"transcript"` when omitted. `rerun` ships in v1 with
//!   `summary.rerun_diff = "not_implemented_yet"` (Wave 2.5 deferral).
//!
//! ### Disabled / not-found paths
//!
//! - **503 `sessions_disabled`** when `AdminState::sessions_disabled`
//!   is set. The UI keys off the 503 status to render the banner. Both
//!   routes share the gate.
//! - **404 `not_found`** with `session_key` echoed back when replay
//!   fails with `SessionNotFound`. The UI renders an inline message
//!   inside the dialog rather than a global toast.
//!
//! ### Tenant scoping
//!
//! Every handler reads the resolved [`TenantId`] from the
//! [`Tenant`](crate::middleware::tenant_scope::Tenant) extractor, so the
//! per-request file path is always `<data_dir>/tenants/<resolved>/sessions.sqlite`.
//! Single-tenant deployments resolve to `TenantId::legacy_default()`,
//! which collapses to the legacy unscoped path segment.

use std::path::PathBuf;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_replay::{list_sessions, replay, ReplayError, ReplayMode, ReplayOutput};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::warn;

use super::AdminState;
use crate::middleware::tenant_scope::Tenant;

/// Wire shape for `GET /admin/sessions`. Mirrors the UI's
/// `SessionsListResponse` in `ui/lib/api/sessions.ts`. `Deserialize` is
/// kept on so this crate's own tests can round-trip the response body
/// without re-stating the shape — it's the real source of truth for
/// the wire contract.
#[derive(Debug, Serialize, Deserialize)]
pub struct SessionsListOut {
    pub sessions: Vec<SessionSummaryOut>,
}

/// One row in `GET /admin/sessions`. Mirrors the UI's `SessionSummary`
/// — `last_message_at` is unix milliseconds (i64).
#[derive(Debug, Serialize, Deserialize)]
pub struct SessionSummaryOut {
    pub session_key: String,
    pub last_message_at: i64,
    pub message_count: i64,
}

/// Body for `POST /admin/sessions/:key/replay`. `mode` defaults to
/// `"transcript"` when omitted, matching the CLI default.
#[derive(Debug, Deserialize, Default)]
pub struct ReplayBody {
    #[serde(default)]
    pub mode: Option<ReplayModeIn>,
}

/// Input enum for the request body. Kept distinct from
/// [`corlinman_replay::ReplayMode`] so a future wire-only mode (e.g.
/// `"diff"`) can land here without touching the primitive.
#[derive(Debug, Deserialize, Clone, Copy)]
#[serde(rename_all = "snake_case")]
pub enum ReplayModeIn {
    Transcript,
    Rerun,
}

impl From<ReplayModeIn> for ReplayMode {
    fn from(m: ReplayModeIn) -> Self {
        match m {
            ReplayModeIn::Transcript => Self::Transcript,
            ReplayModeIn::Rerun => Self::Rerun,
        }
    }
}

/// Sub-router for `/admin/sessions*`. Mounted by
/// [`super::router_with_state`] inside both `require_admin` and
/// `tenant_scope`.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/sessions", get(list_handler))
        .route("/admin/sessions/:key/replay", post(replay_handler))
        .with_state(state)
}

/// Resolve the data directory: prefer the explicit override on
/// [`AdminState`] when set (tests pin it to a tempdir to dodge the
/// global-env race), otherwise fall back to the same `CORLINMAN_DATA_DIR`
/// → `~/.corlinman` chain as `tenants.rs::resolve_data_dir` and
/// `server::resolve_data_dir`. The state-pinned override exists for
/// per-test isolation; production boots leave `state.data_dir = None`
/// and pick up the env var like every other route.
fn resolve_data_dir(state: &AdminState) -> PathBuf {
    if let Some(p) = state.data_dir.as_ref() {
        return p.clone();
    }
    if let Ok(dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".corlinman"))
        .unwrap_or_else(|| PathBuf::from(".corlinman"))
}

fn sessions_disabled_503() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "sessions_disabled",
        })),
    )
        .into_response()
}

fn session_not_found_404(session_key: &str) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({
            "error": "not_found",
            "session_key": session_key,
        })),
    )
        .into_response()
}

fn storage_error(err: impl std::fmt::Display, ctx: &'static str) -> Response {
    warn!(error = %err, "admin/sessions {ctx} failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

async fn list_handler(State(state): State<AdminState>, Tenant(tenant): Tenant) -> Response {
    if state.sessions_disabled {
        return sessions_disabled_503();
    }

    let data_dir = resolve_data_dir(&state);
    match list_sessions(&data_dir, &tenant).await {
        Ok(rows) => {
            let sessions = rows
                .into_iter()
                .map(|r| SessionSummaryOut {
                    session_key: r.session_key,
                    last_message_at: r.last_message_at,
                    message_count: r.message_count,
                })
                .collect();
            (StatusCode::OK, Json(SessionsListOut { sessions })).into_response()
        }
        Err(ReplayError::StoreOpen { .. }) => {
            // No sessions.sqlite for this tenant yet — return an empty
            // list rather than 500. New tenants legitimately hit this
            // path before the first chat lands a row.
            (
                StatusCode::OK,
                Json(SessionsListOut { sessions: vec![] }),
            )
                .into_response()
        }
        Err(other) => storage_error(other, "list"),
    }
}

async fn replay_handler(
    State(state): State<AdminState>,
    Tenant(tenant): Tenant,
    Path(session_key): Path<String>,
    body: Option<Json<ReplayBody>>,
) -> Response {
    if state.sessions_disabled {
        return sessions_disabled_503();
    }

    let mode: ReplayMode = body
        .and_then(|Json(b)| b.mode)
        .map(ReplayMode::from)
        .unwrap_or(ReplayMode::Transcript);

    let data_dir = resolve_data_dir(&state);
    match replay(&data_dir, &tenant, &session_key, mode).await {
        Ok(out) => {
            // The primitive's wire shape already matches the UI
            // contract — pass it through.
            let out: ReplayOutput = out;
            (StatusCode::OK, Json(out)).into_response()
        }
        Err(ReplayError::SessionNotFound(_)) => session_not_found_404(&session_key),
        Err(ReplayError::StoreOpen { .. }) => session_not_found_404(&session_key),
        Err(other) => storage_error(other, "replay"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::{
        config::Config, SessionMessage, SessionRole, SessionStore, SqliteSessionStore,
    };
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_replay::sessions_db_path;
    use corlinman_tenant::TenantId;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn test_state(tmp: &TempDir, disabled: bool) -> AdminState {
        let cfg = Config::default();
        let mut state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        state.sessions_disabled = disabled;
        // Pin the data dir on the state itself rather than the
        // process-global env var so parallel tests don't stomp each
        // other's tempdir between resolve and read.
        state.data_dir = Some(tmp.path().to_path_buf());
        state
    }

    fn msg(role: SessionRole, content: &str, ts_secs: i64) -> SessionMessage {
        SessionMessage {
            role,
            content: content.to_string(),
            tool_call_id: None,
            tool_calls: None,
            ts: time::OffsetDateTime::UNIX_EPOCH + time::Duration::seconds(ts_secs),
        }
    }

    #[tokio::test]
    async fn list_returns_seeded_sessions_descending() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("session-old", msg(SessionRole::User, "old", 1_700_000_000))
            .await
            .unwrap();
        store
            .append("session-new", msg(SessionRole::User, "new", 1_800_000_000))
            .await
            .unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/sessions")
                    .extension(tenant.clone())
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let json: SessionsListOut = serde_json::from_slice(&body).unwrap();
        assert_eq!(json.sessions.len(), 2);
        assert_eq!(json.sessions[0].session_key, "session-new");
        assert_eq!(json.sessions[1].session_key, "session-old");
    }

    #[tokio::test]
    async fn list_returns_503_when_disabled() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let app = router(test_state(&tmp, true));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/sessions")
                    .extension(tenant)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "sessions_disabled");
    }

    #[tokio::test]
    async fn list_empty_when_tenant_has_no_db_yet() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        // Don't seed — the per-tenant directory doesn't exist yet.
        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/sessions")
                    .extension(tenant)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let json: SessionsListOut = serde_json::from_slice(&body).unwrap();
        assert!(json.sessions.is_empty());
    }

    #[tokio::test]
    async fn replay_returns_transcript_for_seeded_session() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("test-session", msg(SessionRole::User, "hi", 1_777_000_000))
            .await
            .unwrap();
        store
            .append(
                "test-session",
                msg(SessionRole::Assistant, "yo", 1_777_000_001),
            )
            .await
            .unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/test-session/replay")
                    .extension(tenant)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"mode":"transcript"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["session_key"], "test-session");
        assert_eq!(v["mode"], "transcript");
        assert_eq!(v["transcript"].as_array().unwrap().len(), 2);
        assert_eq!(v["summary"]["message_count"], 2);
        assert!(
            v["summary"].get("rerun_diff").is_none(),
            "transcript mode must omit rerun_diff",
        );
    }

    #[tokio::test]
    async fn replay_rerun_emits_not_implemented_marker() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("test-session", msg(SessionRole::User, "hi", 1_777_000_000))
            .await
            .unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/test-session/replay")
                    .extension(tenant)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"mode":"rerun"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["mode"], "rerun");
        assert_eq!(v["summary"]["rerun_diff"], "not_implemented_yet");
    }

    #[tokio::test]
    async fn replay_returns_404_for_unknown_session() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let _ = SqliteSessionStore::open(&path).await.unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/ghost/replay")
                    .extension(tenant)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"mode":"transcript"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "not_found");
        assert_eq!(v["session_key"], "ghost");
    }

    #[tokio::test]
    async fn replay_returns_503_when_disabled() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let app = router(test_state(&tmp, true));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/test-session/replay")
                    .extension(tenant)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"mode":"transcript"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn replay_defaults_mode_to_transcript_when_body_omitted() {
        let tmp = TempDir::new().unwrap();

        let tenant = TenantId::legacy_default();
        let path = sessions_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append("test-session", msg(SessionRole::User, "hi", 1_777_000_000))
            .await
            .unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/test-session/replay")
                    .extension(tenant)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["mode"], "transcript");
    }

    #[tokio::test]
    async fn replay_routes_to_per_tenant_path_for_non_default_tenant() {
        let tmp = TempDir::new().unwrap();

        let acme = TenantId::new("acme").unwrap();
        let path = sessions_db_path(tmp.path(), &acme);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let store = SqliteSessionStore::open(&path).await.unwrap();
        store
            .append(
                "acme-session",
                msg(SessionRole::User, "moin", 1_777_000_000),
            )
            .await
            .unwrap();

        let app = router(test_state(&tmp, false));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/sessions/acme-session/replay")
                    .extension(acme)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"mode":"transcript"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["summary"]["tenant_id"], "acme");
    }
}
