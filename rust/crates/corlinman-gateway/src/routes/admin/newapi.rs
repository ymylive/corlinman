//! `/admin/newapi*` — connector for the QuantumNous/new-api sidecar.
//!
//! Routes:
//!
//! - `GET    /admin/newapi`          — summary of the active newapi
//!   provider entry (masked token + admin-key presence + last status).
//!   Returns 503 `no_newapi_provider` when no enabled
//!   `kind = "newapi"` entry is configured.
//! - `GET    /admin/newapi/channels?type={llm|embedding|tts}` — live
//!   channel list pulled from the sidecar's `/api/channel/?type=`
//!   admin API. Used by both `/admin/newapi` page and the onboard
//!   wizard (admin path; onboard goes through `/admin/onboard/*`
//!   instead).
//! - `POST   /admin/newapi/probe`    — validate a candidate
//!   `(base_url, token, admin_token?)` triple without persisting.
//!   Used by the admin UI's "Test connection" button when editing
//!   the connection card before saving.
//! - `POST   /admin/newapi/test`     — issue a 1-token chat
//!   completion against the active newapi entry; reports
//!   `(latency_ms, status, model)`.
//! - `PATCH  /admin/newapi`          — partial update of the active
//!   newapi provider entry. Re-probes the new connection before
//!   accepting; writes atomically through the same admin-write lock
//!   used by `/admin/providers`.
//!
//! Wire shape and error codes match the spec in
//! `docs/superpowers/specs/2026-05-13-newapi-integration-design.md` §5.3.

use std::sync::Arc;

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_core::config::{Config, ProviderEntry, ProviderKind, SecretRef};
use corlinman_newapi_client::{ChannelType, NewapiClient, NewapiError};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::AdminState;

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/newapi", get(get_summary).patch(patch_connection))
        .route("/admin/newapi/channels", get(get_channels))
        .route("/admin/newapi/probe", post(post_probe))
        .route("/admin/newapi/test", post(post_test))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// First enabled `kind = "newapi"` provider in declaration order, plus
/// its slot name. Returns `None` when no such entry exists; callers
/// surface that as 503 `no_newapi_provider`.
fn find_newapi(cfg: &Config) -> Option<(String, ProviderEntry)> {
    for (name, entry) in cfg.providers.iter() {
        let kind = cfg
            .providers
            .kind_for(name, entry)
            .unwrap_or(ProviderKind::OpenaiCompatible);
        if kind == ProviderKind::Newapi && entry.enabled {
            return Some((name.to_string(), entry.clone()));
        }
    }
    None
}

fn mask_token(t: &str) -> String {
    let n = t.len();
    if n <= 8 {
        return "***".into();
    }
    format!("{}...{}", &t[..4], &t[n - 4..])
}

fn resolve_secret(opt: &Option<SecretRef>) -> Option<String> {
    opt.as_ref().and_then(|s| s.resolve().ok())
}

fn map_newapi_err(e: &NewapiError) -> &'static str {
    match e {
        NewapiError::Upstream { status, .. } if *status == 401 => "newapi_token_invalid",
        NewapiError::Upstream { status, .. } if *status == 403 => "newapi_admin_required",
        NewapiError::Upstream { .. } => "newapi_upstream_error",
        NewapiError::NotNewapi => "newapi_version_too_old",
        NewapiError::Http(_) => "newapi_unreachable",
        NewapiError::Url(_) => "newapi_bad_url",
        NewapiError::Json(_) => "newapi_upstream_error",
    }
}

fn bad_request(code: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({ "error": code }))).into_response()
}

// ---------------------------------------------------------------------------
// GET /admin/newapi
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct ConnectionView {
    base_url: String,
    token_masked: String,
    admin_key_present: bool,
    enabled: bool,
}

#[derive(Serialize)]
struct Summary {
    connection: ConnectionView,
    status: &'static str,
}

async fn get_summary(State(state): State<AdminState>) -> Response {
    let cfg = state.config.load_full();
    let Some((_name, entry)) = find_newapi(&cfg) else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "no_newapi_provider" })),
        )
            .into_response();
    };
    let token = resolve_secret(&entry.api_key).unwrap_or_default();
    let admin_key_present = entry
        .params
        .get("newapi_admin_key")
        .map(|v| !v.is_null())
        .unwrap_or(false);
    Json(Summary {
        connection: ConnectionView {
            base_url: entry.base_url.unwrap_or_default(),
            token_masked: mask_token(&token),
            admin_key_present,
            enabled: entry.enabled,
        },
        status: "ok",
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/newapi/probe — no-side-effect connection check
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct ProbeBody {
    base_url: String,
    token: String,
    admin_token: Option<String>,
}

#[derive(Serialize)]
struct ProbeResponse {
    base_url: String,
    user: serde_json::Value,
    server_version: Option<String>,
}

async fn post_probe(State(_state): State<AdminState>, Json(body): Json<ProbeBody>) -> Response {
    let client = match NewapiClient::new(&body.base_url, &body.token, body.admin_token.clone()) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    match client.probe().await {
        Ok(r) => Json(ProbeResponse {
            base_url: r.base_url,
            user: serde_json::to_value(&r.user).unwrap_or(json!({})),
            server_version: r.server_version,
        })
        .into_response(),
        Err(e) => bad_request(map_newapi_err(&e)),
    }
}

// ---------------------------------------------------------------------------
// GET /admin/newapi/channels?type=
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct ChannelsQuery {
    #[serde(rename = "type")]
    pub channel_type: String,
}

async fn get_channels(State(state): State<AdminState>, Query(q): Query<ChannelsQuery>) -> Response {
    let cfg = state.config.load_full();
    let Some((_name, entry)) = find_newapi(&cfg) else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "no_newapi_provider" })),
        )
            .into_response();
    };
    let Some(base_url) = entry.base_url.clone() else {
        return bad_request("newapi_missing_base_url");
    };
    let user_token = resolve_secret(&entry.api_key).unwrap_or_default();
    let admin_token = entry
        .params
        .get("newapi_admin_key")
        .and_then(|v| serde_json::from_value::<SecretRef>(v.clone()).ok())
        .and_then(|s| s.resolve().ok());

    let ct = match q.channel_type.as_str() {
        "llm" => ChannelType::Llm,
        "embedding" => ChannelType::Embedding,
        "tts" => ChannelType::Tts,
        _ => return bad_request("invalid_channel_type"),
    };

    let client = match NewapiClient::new(&base_url, &user_token, admin_token) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    match client.list_channels(ct).await {
        Ok(channels) => Json(json!({ "channels": channels })).into_response(),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({ "error": map_newapi_err(&e) })),
        )
            .into_response(),
    }
}

// ---------------------------------------------------------------------------
// POST /admin/newapi/test
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct TestBody {
    model: String,
}

async fn post_test(State(state): State<AdminState>, Json(body): Json<TestBody>) -> Response {
    let cfg = state.config.load_full();
    let Some((_name, entry)) = find_newapi(&cfg) else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "no_newapi_provider" })),
        )
            .into_response();
    };
    let Some(base_url) = entry.base_url.clone() else {
        return bad_request("newapi_missing_base_url");
    };
    let token = resolve_secret(&entry.api_key).unwrap_or_default();
    let client = match NewapiClient::new(&base_url, &token, None) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    match client.test_round_trip(&body.model).await {
        Ok(r) => Json(json!({
            "status": r.status,
            "latency_ms": r.latency_ms,
            "model": r.model,
        }))
        .into_response(),
        Err(NewapiError::Upstream { status, body }) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({
                "error": "newapi_test_failed",
                "upstream_status": status,
                "body": body,
            })),
        )
            .into_response(),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(json!({
                "error": "newapi_test_failed",
                "detail": e.to_string(),
            })),
        )
            .into_response(),
    }
}

// ---------------------------------------------------------------------------
// PATCH /admin/newapi
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct PatchBody {
    base_url: Option<String>,
    token: Option<String>,
    admin_token: Option<String>,
}

async fn patch_connection(
    State(state): State<AdminState>,
    Json(body): Json<PatchBody>,
) -> Response {
    // Find the active newapi entry; refuse if none exists (operator must
    // create one via /admin/providers POST first, or run onboard).
    let cfg_snapshot = state.config.load_full();
    let Some((name, mut entry)) = find_newapi(&cfg_snapshot) else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "no_newapi_provider" })),
        )
            .into_response();
    };

    if let Some(url) = body.base_url {
        entry.base_url = Some(url);
    }
    if let Some(tok) = body.token {
        entry.api_key = Some(SecretRef::Literal { value: tok });
    }
    if let Some(at) = body.admin_token {
        let secret = SecretRef::Literal { value: at };
        entry.params.insert(
            "newapi_admin_key".into(),
            serde_json::to_value(secret).unwrap_or(json!(null)),
        );
    }

    // Re-probe before persisting. If newapi is now unreachable / the
    // token is bad, reject without touching disk.
    let url = entry.base_url.clone().unwrap_or_default();
    let tok = resolve_secret(&entry.api_key).unwrap_or_default();
    let admin_tok = entry
        .params
        .get("newapi_admin_key")
        .and_then(|v| serde_json::from_value::<SecretRef>(v.clone()).ok())
        .and_then(|s| s.resolve().ok());
    if let Ok(client) = NewapiClient::new(&url, &tok, admin_tok) {
        if let Err(e) = client.probe().await {
            return bad_request(map_newapi_err(&e));
        }
    } else {
        return bad_request("newapi_bad_url");
    }

    // Atomic write through the same admin-write lock as providers.rs.
    let _guard = state.admin_write_lock.lock().await;
    let mut new_cfg = (*state.config.load_full()).clone();
    new_cfg.providers.insert(&name, entry.clone());

    let Some(path) = state.config_path.clone() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "config_path_unset" })),
        )
            .into_response();
    };
    new_cfg.stamp_meta();
    let serialised = match toml::to_string_pretty(&new_cfg) {
        Ok(s) => s,
        Err(err) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": "serialise_failed", "message": err.to_string() })),
            )
                .into_response();
        }
    };
    if let Err(err) = atomic_write(&path, &serialised).await {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": "write_failed", "message": err.to_string() })),
        )
            .into_response();
    }
    state.config.store(Arc::new(new_cfg));
    state.rewrite_py_config().await;
    Json(json!({ "ok": true })).into_response()
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
