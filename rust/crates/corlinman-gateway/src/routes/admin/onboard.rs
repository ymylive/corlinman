//! `/admin/onboard/newapi*` — stateless onboard wizard endpoints.
//!
//! The legacy single-shot `POST /admin/onboard` (in `auth.rs`) creates
//! the admin account. This module adds three stateless endpoints that
//! the 4-step UI wizard hits in sequence:
//!
//!   1. `POST /admin/onboard/newapi/probe`   — UI step 2 "newapi
//!      connect": validates `(base_url, token, admin_token?)` against
//!      a live newapi server without persisting.
//!   2. `POST /admin/onboard/newapi/channels` — UI step 3 "pick
//!      defaults": same body as probe, plus a `type=` filter,
//!      returns the list of channels of that capability.
//!   3. `POST /admin/onboard/finalize`        — UI step 4 "confirm":
//!      atomically writes `[providers.newapi]`, `[models]`,
//!      `[models.aliases.*]`, and `[embedding]` in one TOML write.
//!
//! Server-side session state is deliberately avoided: the UI holds
//! the wizard state in React; each endpoint takes everything it
//! needs inline. Trade-off: the user types the newapi token twice
//! (once for probe, once for finalize). The simplification is worth
//! it — no DashMap, no expiry sweep, no cookie wiring.

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use corlinman_core::config::{
    AliasEntry, AliasSpec, EmbeddingConfig, ProviderEntry, ProviderKind, SecretRef,
};
use corlinman_newapi_client::{ChannelType, NewapiClient, NewapiError};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::AdminState;

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/onboard/newapi/probe", post(post_probe))
        .route("/admin/onboard/newapi/channels", post(post_channels))
        .route("/admin/onboard/finalize", post(post_finalize))
        .with_state(state)
}

fn map_newapi_err(e: &NewapiError) -> &'static str {
    match e {
        NewapiError::Upstream { status: 401, .. } => "newapi_token_invalid",
        NewapiError::Upstream { status: 403, .. } => "newapi_admin_required",
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
// POST /admin/onboard/newapi/probe
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct ProbeBody {
    base_url: String,
    token: String,
    admin_token: Option<String>,
}

#[derive(Serialize)]
struct ProbeResponse {
    next: &'static str,
    base_url: String,
    user: serde_json::Value,
    server_version: Option<String>,
    channels_available: usize,
}

async fn post_probe(State(_state): State<AdminState>, Json(body): Json<ProbeBody>) -> Response {
    let client = match NewapiClient::new(&body.base_url, &body.token, body.admin_token.clone()) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    let probe = match client.probe().await {
        Ok(p) => p,
        Err(e) => return bad_request(map_newapi_err(&e)),
    };
    // Hint: total LLM channels available; the UI's step 3 will list these.
    let count = client
        .list_channels(ChannelType::Llm)
        .await
        .map(|v| v.len())
        .unwrap_or(0);
    Json(ProbeResponse {
        next: "models",
        base_url: probe.base_url,
        user: serde_json::to_value(&probe.user).unwrap_or(json!({})),
        server_version: probe.server_version,
        channels_available: count,
    })
    .into_response()
}

// ---------------------------------------------------------------------------
// POST /admin/onboard/newapi/channels
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct ChannelsBody {
    base_url: String,
    token: String,
    admin_token: Option<String>,
    #[serde(rename = "type")]
    channel_type: String,
}

async fn post_channels(
    State(_state): State<AdminState>,
    Json(body): Json<ChannelsBody>,
) -> Response {
    let ct = match body.channel_type.as_str() {
        "llm" => ChannelType::Llm,
        "embedding" => ChannelType::Embedding,
        "tts" => ChannelType::Tts,
        _ => return bad_request("invalid_channel_type"),
    };
    let client = match NewapiClient::new(&body.base_url, &body.token, body.admin_token.clone()) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    match client.list_channels(ct).await {
        Ok(channels) => Json(json!({ "channels": channels })).into_response(),
        Err(e) => bad_request(map_newapi_err(&e)),
    }
}

// ---------------------------------------------------------------------------
// POST /admin/onboard/finalize
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct FinalizeBody {
    base_url: String,
    token: String,
    admin_token: Option<String>,
    llm: ModelPick,
    embedding: EmbeddingPick,
    /// Optional. When present the value is recorded under
    /// `[providers.newapi.params].newapi_tts_model` for later use
    /// once the voice subsystem is migrated to REST `/v1/audio/speech`.
    /// The currently-shipped `[voice]` block continues to use
    /// `openai-realtime` since the WebSocket realtime API is not
    /// served by new-api.
    #[serde(default)]
    tts: Option<TtsPick>,
}

#[derive(Deserialize)]
struct ModelPick {
    #[serde(default)]
    channel_id: Option<u64>,
    model: String,
}

#[derive(Deserialize)]
struct EmbeddingPick {
    #[serde(default)]
    channel_id: Option<u64>,
    model: String,
    #[serde(default = "default_embed_dim")]
    dimension: u32,
}

fn default_embed_dim() -> u32 {
    1536
}

#[derive(Deserialize)]
struct TtsPick {
    #[serde(default)]
    channel_id: Option<u64>,
    model: String,
    #[serde(default)]
    voice: Option<String>,
}

#[derive(Serialize)]
struct FinalizeResponse {
    ok: bool,
    redirect: &'static str,
}

async fn post_finalize(
    State(state): State<AdminState>,
    Json(body): Json<FinalizeBody>,
) -> Response {
    // Re-probe before persisting; rejects a stale newapi connection
    // discovered between step 2 and step 4 (e.g. operator rotated
    // tokens mid-wizard).
    let client = match NewapiClient::new(&body.base_url, &body.token, body.admin_token.clone()) {
        Ok(c) => c,
        Err(_) => return bad_request("newapi_bad_url"),
    };
    if let Err(e) = client.probe().await {
        return bad_request(map_newapi_err(&e));
    }

    let Some(path) = state.config_path.clone() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": "config_path_unset" })),
        )
            .into_response();
    };

    let _guard = state.admin_write_lock.lock().await;
    let mut new_cfg = (*state.config.load_full()).clone();

    // Build the newapi provider entry.
    let mut params = std::collections::BTreeMap::new();
    let admin_url = format!(
        "{}/api",
        body.base_url.trim_end_matches('/')
    );
    params.insert("newapi_admin_url".into(), json!(admin_url));
    if let Some(at) = body.admin_token.as_ref() {
        let secret = SecretRef::Literal { value: at.clone() };
        if let Ok(v) = serde_json::to_value(secret) {
            params.insert("newapi_admin_key".into(), v);
        }
    }
    if let Some(tts) = body.tts.as_ref() {
        params.insert("newapi_tts_model".into(), json!(tts.model));
        if let Some(v) = tts.voice.as_ref() {
            params.insert("newapi_tts_voice".into(), json!(v));
        }
        if let Some(cid) = tts.channel_id {
            params.insert("newapi_tts_channel_id".into(), json!(cid));
        }
    }
    if let Some(cid) = body.llm.channel_id {
        params.insert("newapi_llm_channel_id".into(), json!(cid));
    }
    if let Some(cid) = body.embedding.channel_id {
        params.insert("newapi_embedding_channel_id".into(), json!(cid));
    }

    let newapi_entry = ProviderEntry {
        kind: Some(ProviderKind::Newapi),
        api_key: Some(SecretRef::Literal {
            value: body.token.clone(),
        }),
        base_url: Some(body.base_url.clone()),
        enabled: true,
        params,
    };
    new_cfg.providers.insert("newapi", newapi_entry);

    // [models] default + alias.
    let mut models_cfg = std::mem::take(&mut new_cfg.models);
    models_cfg.default = body.llm.model.clone();
    models_cfg.aliases.insert(
        body.llm.model.clone(),
        AliasEntry::Full(AliasSpec {
            model: body.llm.model.clone(),
            provider: Some("newapi".into()),
            params: Default::default(),
        }),
    );
    new_cfg.models = models_cfg;

    // [embedding].
    new_cfg.embedding = Some(EmbeddingConfig {
        provider: "newapi".into(),
        model: body.embedding.model.clone(),
        dimension: body.embedding.dimension,
        enabled: true,
        params: Default::default(),
    });

    // [voice]: deliberately NOT changed. The current voice subsystem
    // dials wss://api.openai.com/v1/realtime via
    // routes/voice/provider_openai.rs, not newapi /v1/audio/speech.
    // The TTS pick is recorded under `providers.newapi.params` for
    // later use once voice migrates to REST.

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

    Json(FinalizeResponse {
        ok: true,
        redirect: "/login",
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
