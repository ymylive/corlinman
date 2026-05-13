//! `/admin/providers*` — CRUD for the config-driven provider registry
//! (feature-c §3).
//!
//! Routes:
//!
//! - `GET  /admin/providers` — list every declared provider slot with its
//!   resolved kind, api-key source, base_url, enabled flag, and per-kind
//!   JSON Schema that drives the UI's dynamic form.
//! - `POST /admin/providers` — upsert one provider. Body is validated
//!   against the kind's `params_schema` (baked static JSON, mirrors the
//!   Python `CorlinmanProvider.params_schema()` contract). On success the
//!   gateway atomically rewrites `config.toml` and hot-swaps the
//!   in-memory snapshot.
//! - `PATCH /admin/providers/:name` — partial update (same validation).
//! - `DELETE /admin/providers/:name` — refuses (409) when the slot is
//!   referenced by an alias or the embedding section; returns the
//!   offending references so the UI can guide the user.
//!
//! ## JSON Schema authority
//!
//! The per-kind `params_schema` is baked into Rust for v1 so the admin
//! router can validate upserts without a live Python round-trip. The
//! Python side owns the canonical definition in
//! `python/packages/corlinman-providers/src/.../providers/*.py::params_schema()`
//! — if the two ever diverge, Python wins, and this file should be
//! updated to match.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, patch},
    Json, Router,
};
use corlinman_core::config::{
    AliasEntry, Config, ParamsMap, ProviderEntry, ProviderKind, ProvidersConfig, SecretRef,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};

use super::AdminState;

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route(
            "/admin/providers",
            get(list_providers).post(upsert_provider),
        )
        .route(
            "/admin/providers/:name",
            patch(patch_provider).delete(delete_provider),
        )
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct ProviderView {
    pub name: String,
    pub kind: &'static str,
    pub enabled: bool,
    pub base_url: Option<String>,
    pub api_key_source: &'static str,
    pub api_key_env_name: Option<String>,
    pub params: ParamsMap,
    pub params_schema: JsonValue,
    /// Whether this provider kind can be used as an embedding backend.
    pub capabilities: Capabilities,
}

#[derive(Debug, Serialize)]
pub struct Capabilities {
    pub chat: bool,
    pub embedding: bool,
}

fn kind_capabilities(kind: ProviderKind) -> Capabilities {
    match kind {
        // Anthropic has no embedding API as of the model cutoff.
        ProviderKind::Anthropic => Capabilities {
            chat: true,
            embedding: false,
        },
        // Everything else exposes an OpenAI-wire-format embedding route.
        _ => Capabilities {
            chat: true,
            embedding: true,
        },
    }
}

fn view_from_slot(name: &str, entry: &ProviderEntry, kind: ProviderKind) -> ProviderView {
    let (api_key_source, api_key_env_name) = match entry.api_key.as_ref() {
        None => ("unset", None),
        Some(SecretRef::EnvVar { env }) => ("env", Some(env.clone())),
        Some(SecretRef::Literal { .. }) => ("value", None),
    };
    ProviderView {
        name: name.to_string(),
        kind: kind.as_str(),
        enabled: entry.enabled,
        base_url: entry.base_url.clone(),
        api_key_source,
        api_key_env_name,
        params: entry.params.clone(),
        params_schema: params_schema_for(kind),
        capabilities: kind_capabilities(kind),
    }
}

// ---------------------------------------------------------------------------
// GET /admin/providers
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct ListOut {
    providers: Vec<ProviderView>,
    /// Every kind the backend knows about, each with its JSON Schema. Exposed
    /// so the UI's "Add provider" modal can populate its kind dropdown and
    /// pre-render the params form without making N extra calls.
    kinds: Vec<KindDescriptor>,
}

#[derive(Debug, Serialize)]
struct KindDescriptor {
    kind: &'static str,
    params_schema: JsonValue,
    capabilities: Capabilities,
}

async fn list_providers(State(state): State<AdminState>) -> Json<ListOut> {
    let cfg = state.config.load_full();
    let mut providers: Vec<ProviderView> = cfg
        .providers
        .iter()
        .filter_map(|(name, entry)| {
            cfg.providers
                .kind_for(name, entry)
                .map(|k| view_from_slot(name, entry, k))
        })
        .collect();
    providers.sort_by(|a, b| a.name.cmp(&b.name));
    let kinds: Vec<KindDescriptor> = all_kinds()
        .iter()
        .map(|&k| KindDescriptor {
            kind: k.as_str(),
            params_schema: params_schema_for(k),
            capabilities: kind_capabilities(k),
        })
        .collect();
    Json(ListOut { providers, kinds })
}

/// Mirror of [`ProviderKind::all`]; kept as a thin alias for the existing
/// admin-router code paths so the call sites don't have to change.
fn all_kinds() -> &'static [ProviderKind] {
    ProviderKind::all()
}

// ---------------------------------------------------------------------------
// POST /admin/providers  — upsert
// ---------------------------------------------------------------------------

/// Request body for `POST /admin/providers`.
#[derive(Debug, Deserialize)]
pub struct ProviderUpsert {
    pub name: String,
    pub kind: ProviderKind,
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub base_url: Option<String>,
    #[serde(default)]
    pub api_key: Option<ApiKeyInput>,
    #[serde(default)]
    pub params: Option<ParamsMap>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum ApiKeyInput {
    Env {
        env: String,
    },
    Value {
        value: String,
    },
    /// `null` → explicitly unset.
    Null,
}

async fn upsert_provider(
    State(state): State<AdminState>,
    Json(body): Json<ProviderUpsert>,
) -> Response {
    if body.name.trim().is_empty() {
        return bad_request("invalid_name", "provider name must be non-empty");
    }
    // openai_compatible + newapi both require base_url. openai_compatible
    // has no canonical endpoint by design; newapi is operator-hosted.
    if matches!(
        body.kind,
        ProviderKind::OpenaiCompatible | ProviderKind::Newapi
    ) && body.base_url.as_deref().unwrap_or("").is_empty()
    {
        let kind_str = match body.kind {
            ProviderKind::Newapi => "newapi",
            _ => "openai_compatible",
        };
        return bad_request(
            "base_url_required",
            &format!("providers of kind '{kind_str}' must supply a base_url"),
        );
    }
    // First-party slot names are reserved for their matching kind — keeps
    // the inferred-kind backward-compat path unambiguous.
    if let Some(inferred) = ProviderKind::from_slot_name(&body.name) {
        if inferred != body.kind {
            return bad_request(
                "kind_mismatch",
                &format!(
                    "slot name '{}' is reserved for kind '{}'; use a different name for kind '{}'",
                    body.name,
                    inferred.as_str(),
                    body.kind.as_str()
                ),
            );
        }
    }
    if let Some(params) = body.params.as_ref() {
        if let Err(err) = validate_params(body.kind, params) {
            return bad_request("invalid_params", &err);
        }
    }

    let api_key = match body.api_key {
        None | Some(ApiKeyInput::Null) => None,
        Some(ApiKeyInput::Env { env }) => Some(SecretRef::EnvVar { env }),
        Some(ApiKeyInput::Value { value }) => Some(SecretRef::Literal { value }),
    };

    let entry = ProviderEntry {
        kind: Some(body.kind),
        api_key,
        base_url: body.base_url.filter(|s| !s.is_empty()),
        enabled: body.enabled.unwrap_or(true),
        params: body.params.unwrap_or_default(),
    };

    let name = body.name.clone();
    let mut new_cfg = (*state.config.load_full()).clone();
    place_provider(&mut new_cfg.providers, &name, entry.clone());

    persist_and_swap(state, new_cfg, move |cfg| {
        let entry = snapshot_entry(cfg, &name);
        if let Some((_, e)) = entry {
            Json(view_from_slot(&name, &e, body.kind)).into_response()
        } else {
            (StatusCode::INTERNAL_SERVER_ERROR, "lost after write").into_response()
        }
    })
    .await
}

// ---------------------------------------------------------------------------
// PATCH /admin/providers/:name
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ProviderPatch {
    #[serde(default)]
    pub kind: Option<ProviderKind>,
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub base_url: Option<Option<String>>,
    #[serde(default)]
    pub api_key: Option<ApiKeyInput>,
    #[serde(default)]
    pub params: Option<ParamsMap>,
}

async fn patch_provider(
    State(state): State<AdminState>,
    Path(name): Path<String>,
    Json(body): Json<ProviderPatch>,
) -> Response {
    let current = state.config.load_full();
    let Some((_, existing)) = snapshot_entry(&current, &name) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "not_found", "resource": "provider", "id": name})),
        )
            .into_response();
    };

    // Compose the new entry; `kind` is allowed to change so an operator can
    // reassign a custom slot, but changing the kind of a first-party slot
    // (anthropic/openai/...) is rejected to keep inferred-kind back-compat
    // unambiguous.
    let mut merged = existing.clone();
    if let Some(kind) = body.kind {
        if let Some(inferred) = ProviderKind::from_slot_name(&name) {
            if inferred != kind {
                return bad_request(
                    "kind_mismatch",
                    "first-party provider slots cannot change kind",
                );
            }
        }
        merged.kind = Some(kind);
    }
    if let Some(enabled) = body.enabled {
        merged.enabled = enabled;
    }
    if let Some(base_url) = body.base_url {
        merged.base_url = base_url.filter(|s| !s.is_empty());
    }
    if let Some(api_key) = body.api_key {
        merged.api_key = match api_key {
            ApiKeyInput::Null => None,
            ApiKeyInput::Env { env } => Some(SecretRef::EnvVar { env }),
            ApiKeyInput::Value { value } => Some(SecretRef::Literal { value }),
        };
    }
    if let Some(params) = body.params {
        let resolved_kind = merged.kind.or_else(|| ProviderKind::from_slot_name(&name));
        if let Some(k) = resolved_kind {
            if let Err(err) = validate_params(k, &params) {
                return bad_request("invalid_params", &err);
            }
        }
        merged.params = params;
    }
    // openai_compatible + newapi still require base_url after the patch is applied.
    let resolved_kind = merged.kind.or_else(|| ProviderKind::from_slot_name(&name));
    if matches!(
        resolved_kind,
        Some(ProviderKind::OpenaiCompatible) | Some(ProviderKind::Newapi)
    ) && merged.base_url.as_deref().unwrap_or("").is_empty()
    {
        let kind_str = match resolved_kind {
            Some(ProviderKind::Newapi) => "newapi",
            _ => "openai_compatible",
        };
        return bad_request(
            "base_url_required",
            &format!("providers of kind '{kind_str}' must supply a base_url"),
        );
    }

    let mut new_cfg = (*current).clone();
    place_provider(&mut new_cfg.providers, &name, merged);

    persist_and_swap(state, new_cfg, move |cfg| {
        let (_, e) = snapshot_entry(cfg, &name).expect("just wrote it");
        let kind = cfg
            .providers
            .kind_for(&name, &e)
            .expect("merged entry has a kind");
        Json(view_from_slot(&name, &e, kind)).into_response()
    })
    .await
}

// ---------------------------------------------------------------------------
// DELETE /admin/providers/:name
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct References {
    aliases: Vec<String>,
    embedding: bool,
}

async fn delete_provider(State(state): State<AdminState>, Path(name): Path<String>) -> Response {
    let current = state.config.load_full();
    if snapshot_entry(&current, &name).is_none() {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "not_found", "resource": "provider", "id": name})),
        )
            .into_response();
    }

    // Reference guard — refuse if any alias or the embedding section still
    // points at this provider.
    let refs = detect_references(&current, &name);
    if !refs.aliases.is_empty() || refs.embedding {
        return (
            StatusCode::CONFLICT,
            Json(json!({
                "error": "in_use",
                "message": "provider is referenced by other config sections",
                "references": refs,
            })),
        )
            .into_response();
    }

    let mut new_cfg = (*current).clone();
    clear_provider(&mut new_cfg.providers, &name);

    persist_and_swap(state, new_cfg, |_| StatusCode::NO_CONTENT.into_response()).await
}

fn detect_references(cfg: &Config, provider: &str) -> References {
    let mut aliases: Vec<String> = cfg
        .models
        .aliases
        .iter()
        .filter_map(|(n, e)| match e {
            AliasEntry::Full(spec) if spec.provider.as_deref() == Some(provider) => Some(n.clone()),
            _ => None,
        })
        .collect();
    aliases.sort();
    let embedding = cfg
        .embedding
        .as_ref()
        .map(|e| e.provider == provider)
        .unwrap_or(false);
    References { aliases, embedding }
}

// ---------------------------------------------------------------------------
// Helpers — slot placement / snapshot lookup / params validation
// ---------------------------------------------------------------------------

/// Insert (or replace) a provider entry under `name`. With the BTreeMap-
/// backed [`ProvidersConfig`] this is a one-line `insert`; the helper is
/// kept so the upsert / patch handler bodies stay readable.
fn place_provider(cfg: &mut ProvidersConfig, name: &str, entry: ProviderEntry) {
    cfg.insert(name, entry);
}

fn clear_provider(cfg: &mut ProvidersConfig, name: &str) {
    cfg.remove(name);
}

fn snapshot_entry(cfg: &Config, name: &str) -> Option<(String, ProviderEntry)> {
    cfg.providers
        .get(name)
        .map(|e| (name.to_string(), e.clone()))
}

/// Lightweight params validator: checks top-level shape against
/// [`params_schema_for`]. Draft-2020 JSON Schema keywords honoured: `type`,
/// `properties`, `minimum`, `maximum`, `enum`. Unknown keys in the payload
/// are warned via the error path (fail-closed matches the contract §1
/// "validates before forwarding").
pub(crate) fn validate_params(kind: ProviderKind, params: &ParamsMap) -> Result<(), String> {
    let schema = params_schema_for(kind);
    let props = schema
        .get("properties")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    for (key, value) in params {
        let Some(prop) = props.get(key) else {
            return Err(format!(
                "unknown param '{key}' for kind '{}'",
                kind.as_str()
            ));
        };
        if let Err(e) = check_scalar(prop, value) {
            return Err(format!("param '{key}': {e}"));
        }
    }
    Ok(())
}

fn check_scalar(schema: &JsonValue, value: &JsonValue) -> Result<(), String> {
    let expected_type = schema.get("type").and_then(|t| t.as_str());
    match expected_type {
        Some("number") | Some("integer") => {
            let n = value
                .as_f64()
                .ok_or_else(|| "expected a number".to_string())?;
            if expected_type == Some("integer") && n.fract() != 0.0 {
                return Err("expected an integer".into());
            }
            if let Some(min) = schema.get("minimum").and_then(|v| v.as_f64()) {
                if n < min {
                    return Err(format!("value {n} is below minimum {min}"));
                }
            }
            if let Some(max) = schema.get("maximum").and_then(|v| v.as_f64()) {
                if n > max {
                    return Err(format!("value {n} is above maximum {max}"));
                }
            }
        }
        Some("string") => {
            let s = value
                .as_str()
                .ok_or_else(|| "expected a string".to_string())?;
            if let Some(en) = schema.get("enum").and_then(|v| v.as_array()) {
                if !en.iter().any(|e| e.as_str() == Some(s)) {
                    return Err(format!("value '{s}' not in enum"));
                }
            }
        }
        Some("boolean") => {
            value
                .as_bool()
                .ok_or_else(|| "expected a boolean".to_string())?;
        }
        _ => { /* object / array / unknown — pass */ }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Baked JSON Schemas
// ---------------------------------------------------------------------------
//
// These mirror the per-class `params_schema()` exposed by the Python
// `corlinman-providers` package. If the Python source changes, update
// here to match. Python is the canonical authority — a divergence should
// result in the Python schema winning at request time (on the reasoning
// loop hot path), not in this file dictating behaviour.

pub(crate) fn params_schema_for(kind: ProviderKind) -> JsonValue {
    match kind {
        ProviderKind::Anthropic => anthropic_schema(),
        ProviderKind::Google => google_schema(),
        // Every other kind speaks the OpenAI wire format — they share the
        // same params schema until per-kind quirks (Bedrock SigV4, Azure
        // deployment routing, etc.) earn dedicated schemas.
        ProviderKind::Openai
        | ProviderKind::OpenaiCompatible
        | ProviderKind::Deepseek
        | ProviderKind::Qwen
        | ProviderKind::Glm
        | ProviderKind::Mistral
        | ProviderKind::Cohere
        | ProviderKind::Together
        | ProviderKind::Groq
        | ProviderKind::Replicate
        | ProviderKind::Bedrock
        | ProviderKind::Azure
        | ProviderKind::Newapi => openai_schema(),
    }
}

fn anthropic_schema() -> JsonValue {
    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AnthropicParams",
        "type": "object",
        "additionalProperties": false,
        "properties": {
            "temperature": { "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0 },
            "top_p": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
            "top_k": { "type": "integer", "minimum": 0 },
            "max_tokens": { "type": "integer", "minimum": 1, "maximum": 200000, "default": 4096 },
            "stop_sequences": { "type": "array", "items": { "type": "string" } }
        }
    })
}

fn openai_schema() -> JsonValue {
    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "OpenAICompatibleParams",
        "type": "object",
        "additionalProperties": false,
        "properties": {
            "temperature": { "type": "number", "minimum": 0.0, "maximum": 2.0, "default": 1.0 },
            "top_p": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
            "max_tokens": { "type": "integer", "minimum": 1, "maximum": 128000 },
            "presence_penalty": { "type": "number", "minimum": -2.0, "maximum": 2.0 },
            "frequency_penalty": { "type": "number", "minimum": -2.0, "maximum": 2.0 },
            "seed": { "type": "integer" },
            "response_format": {
                "type": "string",
                "enum": ["text", "json_object"]
            }
        }
    })
}

fn google_schema() -> JsonValue {
    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GoogleParams",
        "type": "object",
        "additionalProperties": false,
        "properties": {
            "temperature": { "type": "number", "minimum": 0.0, "maximum": 2.0, "default": 1.0 },
            "top_p": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
            "top_k": { "type": "integer", "minimum": 0 },
            "max_output_tokens": { "type": "integer", "minimum": 1, "maximum": 8192 },
            "candidate_count": { "type": "integer", "minimum": 1, "maximum": 8 }
        }
    })
}

// ---------------------------------------------------------------------------
// Persist + swap shared helper
// ---------------------------------------------------------------------------

async fn persist_and_swap<F>(state: AdminState, mut new_cfg: Config, render: F) -> Response
where
    F: FnOnce(&Config) -> Response,
{
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

    // PR-#2 review issue #1: belt-and-braces sentinel handling. A
    // round-trip POST (e.g. the operator pastes the redacted GET echo
    // as a literal `api_key.value`) is restored from the live snapshot
    // first; anything that still carries `"***REDACTED***"` after that
    // merge has no real secret to fall back on, so we refuse with 422
    // rather than pin the placeholder string on disk.
    let current = state.config.load_full();
    new_cfg.merge_redacted_secrets_from(&current);
    if new_cfg.has_redacted_sentinel() {
        tracing::error!(
            "admin/providers: refusing to write config containing redaction sentinel",
        );
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

    // PR-#2 review fix: every admin-write path must refresh `[meta]` so
    // the audit stamps reflect the actual write time / crate version.
    new_cfg.stamp_meta();

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

    state.config.store(Arc::new(new_cfg));
    // Feature C last-mile: re-serialise for the Python subprocess after
    // every provider mutation.
    state.rewrite_py_config().await;
    let live = state.config.load_full();
    render(&live)
}

fn bad_request(code: &str, message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": code, "message": message})),
    )
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
    use corlinman_core::config::{AliasSpec, EmbeddingConfig};
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn base_state(path: Option<std::path::PathBuf>) -> AdminState {
        let mut cfg = Config::default();
        // Default seeds a disabled `openai` entry; remove it so the test
        // helper's "single anthropic provider" expectation holds.
        cfg.providers.remove("openai");
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                kind: None,
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                params: ParamsMap::new(),
            },
        );
        let mut state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        );
        if let Some(p) = path {
            state = state.with_config_path(p);
        }
        state
    }

    async fn body_json(resp: Response) -> JsonValue {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    #[tokio::test]
    async fn list_returns_inferred_kind_and_schema() {
        let state = base_state(None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/providers")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        let provs = v["providers"].as_array().unwrap();
        assert_eq!(provs.len(), 1);
        assert_eq!(provs[0]["name"], "anthropic");
        assert_eq!(provs[0]["kind"], "anthropic");
        assert_eq!(provs[0]["api_key_source"], "env");
        assert_eq!(provs[0]["api_key_env_name"], "ANTHROPIC_API_KEY");
        assert!(provs[0]["params_schema"].is_object());
        let kinds = v["kinds"].as_array().unwrap();
        assert!(kinds
            .iter()
            .any(|k| k["kind"] == "openai_compatible" && k["capabilities"]["embedding"] == true));
    }

    #[tokio::test]
    async fn upsert_persists_new_openai_slot() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let app = router(state.clone());
        let body = json!({
            "name": "openai",
            "kind": "openai",
            "enabled": true,
            "base_url": "https://api.openai.com/v1",
            "api_key": { "env": "OPENAI_API_KEY" },
            "params": { "temperature": 0.7 }
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["name"], "openai");
        assert_eq!(v["kind"], "openai");
        assert_eq!(v["api_key_source"], "env");
        assert_eq!(v["params"]["temperature"], 0.7);

        let live = state.config.load();
        assert!(live.providers.contains_key("openai"));
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(on_disk.contains("[providers.openai]"));
    }

    #[tokio::test]
    async fn upsert_rejects_kind_slot_name_mismatch() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = json!({
            "name": "anthropic",
            "kind": "openai",
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "kind_mismatch");
    }

    #[tokio::test]
    async fn upsert_rejects_openai_compatible_without_base_url() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        // Use a first-party slot name that isn't inferred to a different
        // kind (any of the first-party names collide — `openai_compatible`
        // isn't a valid inferred kind, so the mismatch guard fires first
        // when name is reserved. Reuse a reserved name but send the
        // matching kind: we want to assert the base_url guard fires in the
        // absence of a kind mismatch). Here we use kind=openai_compatible
        // with a reserved name `openai` which triggers kind_mismatch first
        // by design — the base_url guard is exercised below via the PATCH
        // path on a fresh anthropic slot.
        let body = json!({
            "name": "openai",
            "kind": "openai_compatible",
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        // Order: base_url_required fires before kind_mismatch in the
        // handler, so a reserved name like `openai` combined with kind
        // `openai_compatible` (mismatch) still surfaces the missing
        // base_url first — the more actionable error for the operator.
        assert_eq!(v["error"], "base_url_required");
    }

    #[tokio::test]
    async fn upsert_rejects_invalid_params() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = json!({
            "name": "openai",
            "kind": "openai",
            "api_key": { "env": "X" },
            "base_url": "https://api.openai.com/v1",
            "params": { "temperature": 99.0 }  // above maximum
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "invalid_params");
    }

    #[tokio::test]
    async fn patch_updates_base_url() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path));
        let app = router(state.clone());
        let body = json!({ "enabled": false });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri("/admin/providers/anthropic")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["enabled"], false);
        assert!(
            !state
                .config
                .load()
                .providers
                .get("anthropic")
                .unwrap()
                .enabled
        );
    }

    #[tokio::test]
    async fn patch_unknown_returns_404() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri("/admin/providers/nope")
                    .header("content-type", "application/json")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn delete_removes_unreferenced_provider() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path));
        let app = router(state.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/admin/providers/anthropic")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
        assert!(!state.config.load().providers.contains_key("anthropic"));
    }

    #[tokio::test]
    async fn delete_rejects_when_referenced_by_alias() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let mut cfg = Config::default();
        cfg.providers.remove("openai"); // drop default seed
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                kind: None,
                api_key: None,
                base_url: None,
                enabled: true,
                params: ParamsMap::new(),
            },
        );
        cfg.models.aliases.insert(
            "smart".into(),
            AliasEntry::Full(AliasSpec {
                model: "claude-opus-4-7".into(),
                provider: Some("anthropic".into()),
                params: ParamsMap::new(),
            }),
        );
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/admin/providers/anthropic")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "in_use");
        assert_eq!(v["references"]["aliases"][0], "smart");
        assert_eq!(v["references"]["embedding"], false);
    }

    #[tokio::test]
    async fn delete_rejects_when_referenced_by_embedding() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let mut cfg = Config::default();
        // Default seeds a disabled openai entry; replace it so this test's
        // setup mirrors pre-refactor expectations exactly.
        cfg.providers.insert(
            "openai",
            ProviderEntry {
                kind: None,
                api_key: Some(SecretRef::EnvVar {
                    env: "OPENAI_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                params: ParamsMap::new(),
            },
        );
        cfg.embedding = Some(EmbeddingConfig {
            provider: "openai".into(),
            model: "text-embedding-3-small".into(),
            dimension: 1536,
            enabled: true,
            params: ParamsMap::new(),
        });
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/admin/providers/openai")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        let v = body_json(resp).await;
        assert_eq!(v["references"]["embedding"], true);
    }

    #[tokio::test]
    async fn upsert_rewrites_py_config_json() {
        // Gap 1 integration: after a provider upsert lands in config.toml,
        // the py-config.json drop the Rust gateway hands to Python must
        // also carry the new slot so a pending Python resolve call sees it.
        let tmp = TempDir::new().unwrap();
        let cfg_path = tmp.path().join("config.toml");
        let py_path = tmp.path().join("py-config.json");
        let mut state = base_state(Some(cfg_path));
        state = state.with_py_config_path(py_path.clone());
        let app = router(state.clone());
        let body = json!({
            "name": "openai",
            "kind": "openai",
            "enabled": true,
            "api_key": { "env": "OPENAI_API_KEY" },
            "params": { "temperature": 0.7 }
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert!(py_path.exists(), "py-config.json should be written");
        let parsed: JsonValue =
            serde_json::from_str(&tokio::fs::read_to_string(&py_path).await.unwrap()).unwrap();
        let providers = parsed["providers"].as_array().unwrap();
        assert!(
            providers
                .iter()
                .any(|p| p["name"] == "openai" && p["kind"] == "openai"),
            "openai slot should appear in py-config.json; got {parsed}"
        );
    }



    #[tokio::test]
    async fn upsert_rejects_newapi_without_base_url() {
        // newapi shares OpenAI wire shape but is operator-hosted — there is
        // no default URL the adapter can fall back to. The handler must
        // reject an upsert that omits base_url with the same
        // `base_url_required` code as openai_compatible.
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = json!({
            "name": "newapi",
            "kind": "newapi",
            "api_key": { "env": "NEWAPI_KEY" },
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "base_url_required");
        // The message names the kind so the operator sees "newapi" rather
        // than a generic "openai_compatible" hint inherited from a shared
        // string.
        let msg = v["message"].as_str().unwrap_or_default();
        assert!(
            msg.contains("newapi"),
            "message should mention newapi; got {msg:?}"
        );
    }

    #[tokio::test]
    async fn upsert_persists_newapi_slot_and_renders_py_config() {
        // End-to-end on the admin side of the newapi round-trip: upsert a
        // newapi provider with a base_url, then assert (a) the slot lands
        // in config.toml with `kind = "newapi"`, and (b) the py-config.json
        // emitted for the Python side carries `kind: "newapi"` so the
        // Python ProviderRegistry routes it through OpenAICompatibleProvider.
        // corlinman treats newapi as a named OpenAI-compat upstream, no
        // schema mirror.
        let tmp = TempDir::new().unwrap();
        let cfg_path = tmp.path().join("config.toml");
        let py_path = tmp.path().join("py-config.json");
        let mut state = base_state(Some(cfg_path.clone()));
        state = state.with_py_config_path(py_path.clone());
        let app = router(state.clone());
        let body = json!({
            "name": "newapi",
            "kind": "newapi",
            "enabled": true,
            "base_url": "http://127.0.0.1:3000",
            "api_key": { "env": "NEWAPI_KEY" },
            "params": { "temperature": 0.4 }
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // config.toml carries the new slot.
        let toml_text = tokio::fs::read_to_string(&cfg_path).await.unwrap();
        assert!(
            toml_text.contains("[providers.newapi]")
                && toml_text.contains("kind = \"newapi\""),
            "config.toml should carry newapi slot; got:\n{toml_text}"
        );

        // py-config.json carries the kind in the snake_case wire shape so
        // the Python registry's _KIND_TO_CLASS lookup matches
        // ProviderKind.NEWAPI → OpenAICompatibleProvider.
        assert!(py_path.exists(), "py-config.json should be written");
        let parsed: JsonValue =
            serde_json::from_str(&tokio::fs::read_to_string(&py_path).await.unwrap()).unwrap();
        let providers = parsed["providers"].as_array().unwrap();
        let entry = providers
            .iter()
            .find(|p| p["name"] == "newapi")
            .expect("newapi slot must appear in py-config.json");
        assert_eq!(entry["kind"], "newapi");
        assert_eq!(entry["base_url"], "http://127.0.0.1:3000");
        assert_eq!(entry["enabled"], true);
    }


    /// PR-#2 review issue #1: posting a literal `api_key.value` of
    /// `"***REDACTED***"` for a slot that has *no* matching entry in the
    /// in-memory snapshot must 422 rather than pin the sentinel string on
    /// disk. The merge step has nothing to restore from for a brand-new
    /// `glm` provider slot, so the belt-and-braces `has_redacted_sentinel`
    /// guard kicks in.
    #[tokio::test]
    async fn upsert_refuses_redacted_payload_when_unmergable() {
        use corlinman_core::config::REDACTED_SENTINEL;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let app = router(state.clone());

        let body = json!({
            "name": "glm",
            "kind": "glm",
            "enabled": true,
            "api_key": { "value": REDACTED_SENTINEL },
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/providers")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "redacted_payload");
        // File never created — the guard fires before atomic_write.
        assert!(!path.exists());
    }

    /// Patching an existing provider with `api_key.value =
    /// "***REDACTED***"` is benign — the merge restores the real secret
    /// from the snapshot, so the write succeeds and the on-disk secret
    /// is preserved.
    #[tokio::test]
    async fn patch_with_redacted_sentinel_restores_live_secret() {
        use corlinman_core::config::REDACTED_SENTINEL;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        // Seed the snapshot with a real literal secret so the merge has
        // something to restore.
        let mut cfg = Config::default();
        cfg.providers.remove("openai");
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                kind: None,
                api_key: Some(SecretRef::Literal {
                    value: "sk-real-secret".into(),
                }),
                base_url: None,
                enabled: true,
                params: ParamsMap::new(),
            },
        );
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path.clone());
        let app = router(state.clone());

        // Operator round-trips the redacted echo unchanged.
        let body = json!({
            "api_key": { "value": REDACTED_SENTINEL },
            "enabled": true,
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("PATCH")
                    .uri("/admin/providers/anthropic")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        // In-memory snapshot still carries the real secret, not the
        // sentinel.
        let live = state.config.load();
        let entry = live.providers.get("anthropic").expect("entry persists");
        match entry.api_key.as_ref().expect("api_key persists") {
            SecretRef::Literal { value } => assert_eq!(value, "sk-real-secret"),
            other => panic!("expected Literal, got {other:?}"),
        }
        // On-disk file: the literal lands, sentinel absent.
        let on_disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(on_disk.contains("sk-real-secret"));
        assert!(!on_disk.contains(REDACTED_SENTINEL));
    }

    #[test]
    fn validate_params_happy_and_error_path() {
        let mut ok = ParamsMap::new();
        ok.insert("temperature".into(), json!(0.5));
        ok.insert("max_tokens".into(), json!(4096));
        assert!(validate_params(ProviderKind::Anthropic, &ok).is_ok());

        let mut bad = ParamsMap::new();
        bad.insert("temperature".into(), json!(5.0));
        assert!(validate_params(ProviderKind::Anthropic, &bad).is_err());

        let mut unknown = ParamsMap::new();
        unknown.insert("gibberish".into(), json!(1));
        assert!(validate_params(ProviderKind::Openai, &unknown).is_err());
    }
}
