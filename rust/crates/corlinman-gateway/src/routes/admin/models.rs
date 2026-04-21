//! `/admin/models*` — model routing / alias management.
//!
//! Sprint 6 T5 (feature-c extended). Routes:
//!
//! - `GET /admin/models` — one snapshot of the active `[providers.*]` slots
//!   (with secrets redacted, `kind` resolved), plus the `[models]` `default` +
//!   `aliases` map including per-alias `provider` / `model` / `params`.
//!
//! - `POST /admin/models/aliases` — upsert a single alias row.
//!   Body: `{ name, provider?, model, params? }`. Persists by rewriting the
//!   whole `config.toml` atomically and hot-swapping the in-memory snapshot.
//!
//! - `DELETE /admin/models/aliases/:name` — remove one alias.
//!
//! All routes live behind the shared admin auth middleware mounted in
//! [`super::router_with_state`].
//!
//! The older bulk-replace `POST /admin/models/aliases` shape
//! (`{ aliases: {...}, default }`) is preserved on the same route via an
//! untagged request body so M6 callers keep working.

use std::collections::{BTreeMap, HashMap};

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use corlinman_core::config::{AliasEntry, AliasSpec, Config, ParamsMap, ProviderEntry, SecretRef};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::AdminState;

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/models", get(list_models))
        .route("/admin/models/aliases", post(upsert_aliases))
        .route("/admin/models/aliases/:name", delete(delete_alias))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// GET /admin/models
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct ProviderRow {
    name: &'static str,
    enabled: bool,
    has_api_key: bool,
    api_key_kind: Option<&'static str>,
    base_url: Option<String>,
    /// Resolved kind — explicit `[providers.*].kind` field if set, else
    /// inferred from the slot name (first-party providers). `None` when
    /// neither was available; the admin UI surfaces this as "unknown".
    kind: Option<&'static str>,
}

impl ProviderRow {
    fn from_entry(
        name: &'static str,
        entry: &ProviderEntry,
        resolved_kind: Option<&'static str>,
    ) -> Self {
        let (has_api_key, api_key_kind) = match entry.api_key.as_ref() {
            None => (false, None),
            Some(SecretRef::EnvVar { .. }) => (true, Some("env")),
            Some(SecretRef::Literal { .. }) => (true, Some("literal")),
        };
        Self {
            name,
            enabled: entry.enabled,
            has_api_key,
            api_key_kind,
            base_url: entry.base_url.clone(),
            kind: resolved_kind,
        }
    }
}

#[derive(Debug, Serialize)]
struct AliasRow {
    name: String,
    model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    provider: Option<String>,
    params: ParamsMap,
}

#[derive(Debug, Serialize)]
struct ModelsResponse {
    default: String,
    aliases: Vec<AliasRow>,
    providers: Vec<ProviderRow>,
}

async fn list_models(State(state): State<AdminState>) -> Json<ModelsResponse> {
    let cfg = state.config.load_full();
    let providers: Vec<ProviderRow> = cfg
        .providers
        .iter()
        .map(|(n, e)| {
            let kind = cfg.providers.kind_for(n, e).map(|k| k.as_str());
            ProviderRow::from_entry(n, e, kind)
        })
        .collect();
    let mut aliases: Vec<AliasRow> = cfg
        .models
        .aliases
        .iter()
        .map(|(name, entry)| AliasRow {
            name: name.clone(),
            model: entry.target().to_string(),
            provider: entry.provider().map(str::to_string),
            params: entry.params().clone(),
        })
        .collect();
    aliases.sort_by(|a, b| a.name.cmp(&b.name));
    Json(ModelsResponse {
        default: cfg.models.default.clone(),
        aliases,
        providers,
    })
}

// ---------------------------------------------------------------------------
// POST /admin/models/aliases
// ---------------------------------------------------------------------------

/// Request body for `POST /admin/models/aliases`. Two shapes:
///
/// - **Single upsert** (feature-c UI):
///   `{ "name": "smart", "model": "claude-opus-4-7", "provider": "anthropic",
///     "params": {"temperature": 0.7} }`
///   → updates/creates one alias, leaves the rest untouched.
///
/// - **Bulk replace** (legacy M6):
///   `{ "aliases": {"smart": "claude-opus-4-7"}, "default": "..." }`
///   → replaces the full alias map + optional default.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum AliasesBody {
    Single(AliasUpsert),
    Bulk(BulkAliasesBody),
}

#[derive(Debug, Deserialize)]
pub struct AliasUpsert {
    pub name: String,
    pub model: String,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub params: Option<ParamsMap>,
}

#[derive(Debug, Deserialize)]
pub struct BulkAliasesBody {
    /// Full desired alias map (replaces, not merges — drop an entry by
    /// omitting it, add by including it).
    pub aliases: HashMap<String, String>,
    #[serde(default)]
    pub default: Option<String>,
}

async fn upsert_aliases(
    State(state): State<AdminState>,
    Json(body): Json<AliasesBody>,
) -> Response {
    match body {
        AliasesBody::Single(up) => apply_single_upsert(state, up).await,
        AliasesBody::Bulk(bulk) => apply_bulk_replace(state, bulk).await,
    }
}

async fn apply_single_upsert(state: AdminState, up: AliasUpsert) -> Response {
    if up.name.is_empty() || up.model.is_empty() {
        return bad_request("invalid_alias", "alias name and model must be non-empty");
    }
    if let Some(p) = up.provider.as_ref() {
        if p.is_empty() {
            return bad_request(
                "invalid_provider",
                "alias provider must be non-empty when supplied",
            );
        }
    }

    let params = up.params.unwrap_or_default();
    let entry = if up.provider.is_some() || !params.is_empty() {
        AliasEntry::Full(AliasSpec {
            model: up.model,
            provider: up.provider,
            params,
        })
    } else {
        AliasEntry::Shorthand(up.model)
    };

    let mut new_cfg: Config = (*state.config.load_full()).clone();
    new_cfg
        .models
        .aliases
        .insert(up.name.clone(), entry.clone());

    persist_and_swap(state, new_cfg, move |cfg| {
        Json(single_row(&up.name, cfg)).into_response()
    })
    .await
}

async fn apply_bulk_replace(state: AdminState, body: BulkAliasesBody) -> Response {
    for (k, v) in &body.aliases {
        if k.is_empty() || v.is_empty() {
            return bad_request("invalid_alias", "alias name and target must be non-empty");
        }
    }
    if let Some(d) = body.default.as_ref() {
        if d.is_empty() {
            return bad_request("invalid_default", "default model must be non-empty");
        }
    }

    let mut new_cfg: Config = (*state.config.load_full()).clone();
    new_cfg.models.aliases = body
        .aliases
        .into_iter()
        .map(|(k, v)| (k, AliasEntry::Shorthand(v)))
        .collect();
    if let Some(d) = body.default.clone() {
        new_cfg.models.default = d;
    }

    persist_and_swap(state, new_cfg, |cfg| {
        let aliases: BTreeMap<String, String> = cfg
            .models
            .aliases
            .iter()
            .map(|(k, e)| (k.clone(), e.target().to_string()))
            .collect();
        Json(json!({
            "status": "ok",
            "default": cfg.models.default,
            "aliases": aliases,
        }))
        .into_response()
    })
    .await
}

fn single_row(name: &str, cfg: &Config) -> AliasRow {
    let entry = cfg.models.aliases.get(name);
    AliasRow {
        name: name.to_string(),
        model: entry.map(|e| e.target().to_string()).unwrap_or_default(),
        provider: entry.and_then(|e| e.provider()).map(str::to_string),
        params: entry.map(|e| e.params().clone()).unwrap_or_default(),
    }
}

// ---------------------------------------------------------------------------
// DELETE /admin/models/aliases/:name
// ---------------------------------------------------------------------------

async fn delete_alias(State(state): State<AdminState>, Path(name): Path<String>) -> Response {
    let current = state.config.load_full();
    if !current.models.aliases.contains_key(&name) {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "not_found", "resource": "alias", "id": name})),
        )
            .into_response();
    }
    let mut new_cfg: Config = (*current).clone();
    new_cfg.models.aliases.remove(&name);
    persist_and_swap(state, new_cfg, |_| StatusCode::NO_CONTENT.into_response()).await
}

// ---------------------------------------------------------------------------
// Shared persist helper
// ---------------------------------------------------------------------------

async fn persist_and_swap<F>(state: AdminState, new_cfg: Config, render: F) -> Response
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

    state.config.store(std::sync::Arc::new(new_cfg));
    // Feature C last-mile: re-serialise for the Python subprocess after
    // every alias mutation.
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
    use corlinman_core::config::{Config, ProviderEntry, SecretRef};
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn base_state(path: Option<std::path::PathBuf>) -> AdminState {
        let mut cfg = Config::default();
        cfg.providers.anthropic = Some(ProviderEntry {
            api_key: Some(SecretRef::EnvVar {
                env: "ANTHROPIC_API_KEY".into(),
            }),
            base_url: None,
            enabled: true,
            ..Default::default()
        });
        cfg.providers.openai = Some(ProviderEntry {
            api_key: None,
            base_url: Some("https://openai.example".into()),
            enabled: false,
            ..Default::default()
        });
        cfg.models.aliases.insert(
            "smart".into(),
            AliasEntry::Shorthand("claude-opus-4-7".into()),
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

    async fn body_json(resp: Response) -> serde_json::Value {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    #[tokio::test]
    async fn list_returns_providers_and_aliases() {
        let state = base_state(None);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/models")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["default"], "claude-sonnet-4-5");
        let aliases = v["aliases"].as_array().unwrap();
        assert_eq!(aliases.len(), 1);
        assert_eq!(aliases[0]["name"], "smart");
        assert_eq!(aliases[0]["model"], "claude-opus-4-7");
        assert!(aliases[0]["params"].as_object().unwrap().is_empty());
        let providers = v["providers"].as_array().unwrap();
        assert!(providers
            .iter()
            .any(|p| p["name"] == "anthropic" && p["enabled"] == true && p["kind"] == "anthropic"));
        assert!(providers
            .iter()
            .any(|p| p["name"] == "openai" && p["has_api_key"] == false));
    }

    #[tokio::test]
    async fn upsert_alias_single_row_persists_params() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let app = router(state.clone());
        let body = serde_json::json!({
            "name": "creative",
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "params": { "temperature": 0.9, "max_tokens": 4096 }
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/models/aliases")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["name"], "creative");
        assert_eq!(v["provider"], "anthropic");
        assert_eq!(v["params"]["temperature"], 0.9);

        // Snapshot updated; existing alias preserved.
        let live = state.config.load();
        let creative = live.models.aliases.get("creative").unwrap();
        assert_eq!(creative.target(), "claude-opus-4-7");
        assert_eq!(creative.provider(), Some("anthropic"));
        assert!(live.models.aliases.contains_key("smart"));
        // Round-tripped on disk.
        let text = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(text.contains("creative"));
        assert!(text.contains("temperature"));
    }

    #[tokio::test]
    async fn upsert_alias_bulk_replace_mode() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let app = router(state.clone());
        let body = serde_json::to_string(&serde_json::json!({
            "aliases": {"fast": "claude-haiku", "smart": "claude-opus-4-7"},
            "default": "claude-opus-4-7",
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/models/aliases")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["default"], "claude-opus-4-7");
        assert_eq!(v["aliases"]["fast"], "claude-haiku");

        // Snapshot updated.
        let live = state.config.load();
        assert_eq!(live.models.default, "claude-opus-4-7");
        assert_eq!(
            live.models.aliases.get("fast").unwrap().target(),
            "claude-haiku"
        );
        // File persisted.
        assert!(path.exists());
    }

    #[tokio::test]
    async fn upsert_alias_rejects_empty_name() {
        let tmp = TempDir::new().unwrap();
        let state = base_state(Some(tmp.path().join("config.toml")));
        let app = router(state);
        let body = serde_json::to_string(&serde_json::json!({
            "aliases": {"": "x"},
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/models/aliases")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn upsert_alias_returns_503_without_config_path() {
        let state = base_state(None);
        let app = router(state);
        let body = serde_json::json!({
            "name": "creative",
            "model": "claude-opus-4-7"
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/models/aliases")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn delete_alias_removes_row() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path.clone()));
        let app = router(state.clone());
        let resp = app
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/admin/models/aliases/smart")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
        assert!(!state.config.load().models.aliases.contains_key("smart"));
    }

    #[tokio::test]
    async fn delete_alias_returns_404_for_unknown() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = base_state(Some(path));
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/admin/models/aliases/nope")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }
}
