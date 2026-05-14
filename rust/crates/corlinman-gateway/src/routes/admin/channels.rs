//! `/admin/channels/qq*` — QQ/OneBot channel management.
//!
//! Sprint 6 T2. Three routes:
//!
//! - `GET /admin/channels/qq/status` — configuration snapshot + recent-message
//!   placeholder. Runtime connection state (ws handshake, heartbeat) isn't
//!   yet tracked by [`corlinman_channels::qq`] — the OneBot client's `run()`
//!   loop owns the socket state without publishing it. Until that surface
//!   lands the status endpoint returns `runtime = "unknown"` with an explicit
//!   field so the UI can render a neutral indicator rather than lying about
//!   connectivity. See `corlinman-channels/src/qq/onebot.rs` TODO.
//!
//! - `POST /admin/channels/qq/reconnect` — placeholder. The current OneBot
//!   client handles reconnects internally via `reconnect_schedule`, so there
//!   is no in-process hook to force a fresh connect attempt. The route
//!   returns 501 `not_implemented` with a clear message so the UI can
//!   disable the button on a real gateway; the control surface is tracked
//!   for a follow-up.
//!
//! - `POST /admin/channels/qq/keywords` — updates `channels.qq.group_keywords`
//!   in the active config. Writes the file atomically and swaps the in-memory
//!   snapshot so the router's next lookup sees the new keywords (the lookup
//!   reads from the live config on each inbound message).
//!
//! All routes require an `AdminState` with `config_path` set for the
//! write-path; `GET status` works even in a stripped-down harness.

use std::collections::HashMap;

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::AdminState;

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/channels/qq/status", get(status))
        .route("/admin/channels/qq/reconnect", post(reconnect))
        .route("/admin/channels/qq/keywords", post(update_keywords))
        // v0.3 — QQ scan-login proxy against NapCat's webui. See
        // `super::napcat` for the API-version assumption + account
        // history file format.
        .route("/admin/channels/qq/qrcode", post(super::napcat::qrcode))
        .route(
            "/admin/channels/qq/qrcode/status",
            get(super::napcat::qrcode_status),
        )
        .route("/admin/channels/qq/accounts", get(super::napcat::accounts))
        .route(
            "/admin/channels/qq/quick-login",
            post(super::napcat::quick_login),
        )
        .with_state(state)
}

// ---------------------------------------------------------------------------
// GET /admin/channels/qq/status
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct StatusOut {
    configured: bool,
    enabled: bool,
    ws_url: Option<String>,
    self_ids: Vec<i64>,
    group_keywords: HashMap<String, Vec<String>>,
    /// Connection state as known to the admin surface. Always "unknown"
    /// until corlinman-channels publishes live status (tracked in
    /// `corlinman-channels/src/qq/onebot.rs`).
    runtime: &'static str,
    /// Recent inbound messages — not yet wired; the OneBot client doesn't
    /// expose an inbox snapshot. Empty list so the UI can render the panel
    /// without a null guard.
    recent_messages: Vec<serde_json::Value>,
}

async fn status(State(state): State<AdminState>) -> Json<StatusOut> {
    let cfg = state.config.load_full();
    let qq = cfg.channels.qq.as_ref();
    let (configured, enabled, ws_url, self_ids, keywords) = match qq {
        None => (false, false, None, Vec::new(), HashMap::new()),
        Some(q) => (
            true,
            q.enabled,
            Some(q.ws_url.clone()),
            q.self_ids.clone(),
            q.group_keywords.clone(),
        ),
    };
    Json(StatusOut {
        configured,
        enabled,
        ws_url,
        self_ids,
        group_keywords: keywords,
        runtime: "unknown",
        recent_messages: Vec::new(),
    })
}

// ---------------------------------------------------------------------------
// POST /admin/channels/qq/reconnect
// ---------------------------------------------------------------------------

async fn reconnect(State(state): State<AdminState>) -> Response {
    let cfg = state.config.load_full();
    if cfg.channels.qq.is_none() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "channel_not_configured",
                "message": "no [channels.qq] section in config",
            })),
        )
            .into_response();
    }
    // The OneBot client owns the WebSocket loop + reconnect schedule
    // internally; there's no public handle today to poke it. Return
    // 501 so the UI disables the button on the real gateway.
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({
            "error": "reconnect_unsupported",
            "message": "force-reconnect control is not yet implemented; the OneBot client handles reconnect internally",
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/channels/qq/keywords
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct KeywordsBody {
    /// Full replacement map: `group_id → [keyword, …]`. Pass an empty
    /// object to clear all overrides.
    pub group_keywords: HashMap<String, Vec<String>>,
}

#[derive(Debug, Serialize)]
struct KeywordsOut {
    status: &'static str,
    group_keywords: HashMap<String, Vec<String>>,
}

async fn update_keywords(
    State(state): State<AdminState>,
    Json(body): Json<KeywordsBody>,
) -> Response {
    // Reject empty-string keys / keywords up front.
    for (group, kws) in &body.group_keywords {
        if group.is_empty() {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "invalid_group", "message": "group id must be non-empty"})),
            )
                .into_response();
        }
        if kws.iter().any(|k| k.is_empty()) {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "invalid_keyword", "message": "keyword must be non-empty"})),
            )
                .into_response();
        }
    }

    let mut new_cfg = (*state.config.load_full()).clone();
    let qq = match new_cfg.channels.qq.as_mut() {
        Some(q) => q,
        None => {
            // Insert a disabled skeleton so operators can seed keywords before
            // flipping `enabled`. `ws_url` has no sensible default — refuse.
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({
                    "error": "channel_not_configured",
                    "message": "[channels.qq] missing; add a stub in config.toml before editing keywords",
                })),
            )
                .into_response();
        }
    };
    qq.group_keywords = body.group_keywords.clone();

    // PR-#2 review issue #1: belt-and-braces sentinel guard. The
    // keywords payload itself doesn't carry secrets, but the cloned
    // snapshot could carry `"***REDACTED***"` from a botched earlier
    // round-trip — restore from the live snapshot first, then refuse
    // to persist anything that still pins the placeholder string on
    // disk.
    let current = state.config.load_full();
    new_cfg.merge_redacted_secrets_from(&current);
    if new_cfg.has_redacted_sentinel() {
        tracing::error!("admin/channels: refusing to write config containing redaction sentinel",);
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "redacted_payload",
                "message": "payload contains the literal `***REDACTED***` placeholder for at least one secret. \
                            Replace it with a real value (or omit the field to keep the current secret) before retrying.",
            })),
        )
            .into_response();
    }

    // PR-#2 review fix: refresh `[meta]` audit stamps before serialise.
    new_cfg.stamp_meta();

    let Some(path) = state.config_path.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "config_path_unset",
                "message": "gateway booted without a config file path",
            })),
        )
            .into_response();
    };
    let serialised = match toml::to_string_pretty(&new_cfg) {
        Ok(s) => s,
        Err(err) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "serialise_failed", "message": err.to_string()})),
            )
                .into_response();
        }
    };
    if let Err(err) = atomic_write(path, &serialised).await {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "write_failed", "message": err.to_string()})),
        )
            .into_response();
    }

    state.config.store(std::sync::Arc::new(new_cfg.clone()));
    let updated: HashMap<String, Vec<String>> = new_cfg
        .channels
        .qq
        .as_ref()
        .map(|q| q.group_keywords.clone())
        .unwrap_or_default();
    Json(KeywordsOut {
        status: "ok",
        group_keywords: updated,
    })
    .into_response()
}

async fn atomic_write(path: &std::path::Path, contents: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let mut tmp = path.to_path_buf();
    tmp.as_mut_os_string().push(".new");
    tokio::fs::write(&tmp, contents).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::{Config, QqChannelConfig, QqRateLimit};
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn cfg_with_qq() -> Config {
        let mut cfg = Config::default();
        cfg.channels.qq = Some(QqChannelConfig {
            enabled: true,
            ws_url: "ws://127.0.0.1:3001".into(),
            access_token: None,
            self_ids: vec![42],
            group_keywords: HashMap::new(),
            rate_limit: QqRateLimit::default(),
            napcat_url: None,
            napcat_access_token: None,
        });
        cfg
    }

    fn state_with(cfg: Config, path: Option<std::path::PathBuf>) -> AdminState {
        let mut s = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        if let Some(p) = path {
            s = s.with_config_path(p);
        }
        s
    }

    async fn body_json(resp: Response) -> serde_json::Value {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    #[tokio::test]
    async fn status_reports_configured_when_qq_set() {
        let state = state_with(cfg_with_qq(), None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/channels/qq/status")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["configured"], true);
        assert_eq!(v["enabled"], true);
        assert_eq!(v["ws_url"], "ws://127.0.0.1:3001");
        assert_eq!(v["runtime"], "unknown");
    }

    #[tokio::test]
    async fn status_reports_unconfigured_when_missing() {
        let state = state_with(Config::default(), None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/channels/qq/status")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["configured"], false);
        assert_eq!(v["enabled"], false);
    }

    #[tokio::test]
    async fn reconnect_returns_501_when_configured() {
        let state = state_with(cfg_with_qq(), None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/reconnect")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
    }

    #[tokio::test]
    async fn update_keywords_swaps_snapshot_and_writes_file() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = state_with(cfg_with_qq(), Some(path.clone()));
        let app = router(state.clone());

        let body = serde_json::to_string(&serde_json::json!({
            "group_keywords": {"1001": ["hello", "hi"], "1002": ["ping"]},
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/keywords")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let live = state.config.load();
        let qq = live.channels.qq.as_ref().unwrap();
        assert_eq!(
            qq.group_keywords.get("1001").unwrap(),
            &vec!["hello".to_string(), "hi".to_string()]
        );
        assert!(path.exists());
    }

    /// PR-#2 review issue #1: refusal path when the live snapshot
    /// carries the redaction sentinel — e.g. the QQ access_token was
    /// somehow left as `"***REDACTED***"` in memory. The merge has
    /// nothing real to restore from, so the keywords write must 422
    /// rather than pin the placeholder on disk.
    #[tokio::test]
    async fn update_keywords_refuses_when_snapshot_carries_sentinel() {
        use corlinman_core::config::{SecretRef, REDACTED_SENTINEL};

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let mut cfg = cfg_with_qq();
        // Set the QQ access_token to the literal sentinel — simulates a
        // botched earlier round-trip that landed the redacted echo in
        // live state.
        cfg.channels.qq.as_mut().unwrap().access_token = Some(SecretRef::Literal {
            value: REDACTED_SENTINEL.into(),
        });
        let state = state_with(cfg, Some(path.clone()));
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "group_keywords": {"1001": ["hello"]},
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/keywords")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "redacted_payload");
        assert!(!path.exists());
    }

    #[tokio::test]
    async fn update_keywords_rejects_empty_keyword() {
        let tmp = TempDir::new().unwrap();
        let state = state_with(cfg_with_qq(), Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "group_keywords": {"1001": [""]},
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/keywords")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
