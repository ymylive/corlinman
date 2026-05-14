//! `/admin/embedding*` — embedding provider configuration + benchmark
//! passthrough (feature-c §3).
//!
//! Routes:
//!
//! - `GET  /admin/embedding` — current `[embedding]` section (or 404 if
//!   absent). The response includes the `params_schema` of the referenced
//!   provider kind so the UI's dynamic form knows what to render.
//!
//! - `POST /admin/embedding` — set the `[embedding]` section. Validates
//!   the referenced provider exists and the params round-trip through its
//!   kind-level JSON Schema.
//!
//! - `POST /admin/embedding/benchmark` — compute a similarity matrix +
//!   latency report for a set of sample strings. Passes through to the
//!   Python admin sidecar at `$CORLINMAN_PY_ADMIN_URL` (default
//!   `http://127.0.0.1:50052`). Connect failures surface as **503
//!   `python_sidecar_unavailable`** so the UI can distinguish "Python
//!   offline" from client-side validation errors.

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_core::config::{Config, EmbeddingConfig, ParamsMap};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};

use super::providers::{params_schema_for, validate_params};
use super::AdminState;

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/embedding", get(get_embedding).post(post_embedding))
        .route("/admin/embedding/benchmark", post(post_benchmark))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct EmbeddingView {
    pub provider: String,
    pub model: String,
    pub dimension: u32,
    pub enabled: bool,
    pub params: ParamsMap,
    pub params_schema: JsonValue,
}

fn render_view(cfg: &Config) -> Option<EmbeddingView> {
    let emb = cfg.embedding.as_ref()?;
    let kind = cfg
        .providers
        .iter()
        .find(|(n, _)| *n == emb.provider)
        .and_then(|(n, e)| cfg.providers.kind_for(n, e));
    let params_schema = kind.map(params_schema_for).unwrap_or_else(|| json!({}));
    Some(EmbeddingView {
        provider: emb.provider.clone(),
        model: emb.model.clone(),
        dimension: emb.dimension,
        enabled: emb.enabled,
        params: emb.params.clone(),
        params_schema,
    })
}

// ---------------------------------------------------------------------------
// GET /admin/embedding
// ---------------------------------------------------------------------------

async fn get_embedding(State(state): State<AdminState>) -> Response {
    let cfg = state.config.load_full();
    match render_view(&cfg) {
        Some(view) => Json(view).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "not_configured",
                "message": "no [embedding] section configured",
            })),
        )
            .into_response(),
    }
}

// ---------------------------------------------------------------------------
// POST /admin/embedding  — upsert
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct EmbeddingUpsert {
    pub provider: String,
    pub model: String,
    pub dimension: u32,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub params: ParamsMap,
}

fn default_true() -> bool {
    true
}

async fn post_embedding(
    State(state): State<AdminState>,
    Json(body): Json<EmbeddingUpsert>,
) -> Response {
    if body.provider.trim().is_empty() {
        return bad_request("invalid_provider", "provider must be non-empty");
    }
    if body.model.trim().is_empty() {
        return bad_request("invalid_model", "model must be non-empty");
    }
    if body.dimension == 0 {
        return bad_request("invalid_dimension", "dimension must be > 0");
    }

    let current = state.config.load_full();

    // Provider must exist among the declared slots.
    let Some((slot_name, slot_entry)) = current.providers.iter().find(|(n, _)| *n == body.provider)
    else {
        return bad_request(
            "provider_missing",
            &format!("no [providers.{}] block declared", body.provider),
        );
    };
    let Some(kind) = current.providers.kind_for(slot_name, slot_entry) else {
        return bad_request(
            "provider_kind_unknown",
            &format!(
                "provider '{}' has no resolvable kind (set `kind = ...` explicitly)",
                body.provider
            ),
        );
    };
    if let Err(err) = validate_params(kind, &body.params) {
        return bad_request("invalid_params", &err);
    }

    let entry = EmbeddingConfig {
        provider: body.provider.clone(),
        model: body.model,
        dimension: body.dimension,
        enabled: body.enabled,
        params: body.params,
    };

    let mut new_cfg = (*current).clone();
    new_cfg.embedding = Some(entry);

    persist_and_swap(state, new_cfg, |cfg| match render_view(cfg) {
        Some(v) => Json(v).into_response(),
        None => (StatusCode::INTERNAL_SERVER_ERROR, "lost after write").into_response(),
    })
    .await
}

// ---------------------------------------------------------------------------
// POST /admin/embedding/benchmark — pending python
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct BenchmarkBody {
    pub samples: Vec<String>,
    #[serde(default)]
    pub dimension: Option<u32>,
    #[serde(default)]
    pub params: Option<ParamsMap>,
    /// Unused — retained for backward-compat with earlier UI builds.
    #[serde(default)]
    pub limit: Option<usize>,
}

/// Env var overriding the Python admin sidecar address (used in tests).
const ENV_PY_ADMIN_URL: &str = "CORLINMAN_PY_ADMIN_URL";
const DEFAULT_PY_ADMIN_URL: &str = "http://127.0.0.1:50052";
const BENCHMARK_TIMEOUT_SECS: u64 = 60;

async fn post_benchmark(
    State(_state): State<AdminState>,
    Json(body): Json<BenchmarkBody>,
) -> Response {
    if body.samples.is_empty() {
        return bad_request("invalid_samples", "samples must be non-empty");
    }
    if body.samples.len() > 20 {
        return bad_request(
            "too_many_samples",
            "samples is capped at 20 entries per benchmark call",
        );
    }

    // Forward to the Python admin sidecar — a localhost HTTP endpoint that
    // runs in the `corlinman-python-server` process. Connection errors are
    // surfaced as 503 so the UI can distinguish "Python offline" from
    // client-side validation failures.
    let base = std::env::var(ENV_PY_ADMIN_URL).unwrap_or_else(|_| DEFAULT_PY_ADMIN_URL.to_string());
    let url = format!("{}/embedding/benchmark", base.trim_end_matches('/'));

    let mut payload = serde_json::Map::new();
    payload.insert("samples".into(), serde_json::json!(body.samples));
    if let Some(d) = body.dimension {
        payload.insert("dimension".into(), serde_json::json!(d));
    }
    if let Some(p) = body.params {
        payload.insert("params".into(), serde_json::to_value(p).unwrap_or_default());
    }

    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(BENCHMARK_TIMEOUT_SECS))
        .build()
    {
        Ok(c) => c,
        Err(err) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "client_build_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    let resp = match client.post(&url).json(&payload).send().await {
        Ok(r) => r,
        Err(err) => {
            tracing::warn!(error = %err, url = %url, "embedding benchmark: sidecar unreachable");
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({
                    "error": "python_sidecar_unavailable",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    let status = resp.status();
    let body_bytes = match resp.bytes().await {
        Ok(b) => b,
        Err(err) => {
            return (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": "python_sidecar_read_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    // Pass through the sidecar's JSON verbatim — success or error — so the
    // shape the Python side emits (the `BenchmarkView` contract on success,
    // `{error, message}` on failure) reaches the caller unchanged. We only
    // remap the HTTP status when reqwest can't translate it; otherwise
    // axum emits whatever the Python side chose.
    let status_code = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let json_body: serde_json::Value = serde_json::from_slice(&body_bytes)
        .unwrap_or_else(|_| json!({"raw": String::from_utf8_lossy(&body_bytes)}));
    (status_code, Json(json_body)).into_response()
}

// ---------------------------------------------------------------------------
// Shared helpers
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

    // PR-#2 review issue #1: belt-and-braces sentinel guard. The
    // embedding payload itself doesn't carry secrets, but the route
    // clones the live snapshot before mutating — if any sibling
    // section (provider api_key, channel access_token) has somehow
    // landed `"***REDACTED***"` in memory, restore it from the
    // current snapshot and refuse to persist anything that still
    // pins the placeholder on disk.
    let current = state.config.load_full();
    new_cfg.merge_redacted_secrets_from(&current);
    if new_cfg.has_redacted_sentinel() {
        tracing::error!("admin/embedding: refusing to write config containing redaction sentinel",);
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

    // PR-#2 review fix: refresh `[meta]` before serialising so the
    // audit stamps survive every admin write, not just the boot-time
    // `Config::save_to_path` callers.
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

    state.config.store(std::sync::Arc::new(new_cfg));
    // Feature C last-mile: re-serialise for the Python subprocess after
    // every embedding mutation.
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
    use corlinman_core::config::{EmbeddingConfig, ProviderEntry, SecretRef};
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn state_with_openai(path: Option<std::path::PathBuf>, seed_embed: bool) -> AdminState {
        let mut cfg = Config::default();
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
        if seed_embed {
            cfg.embedding = Some(EmbeddingConfig {
                provider: "openai".into(),
                model: "text-embedding-3-small".into(),
                dimension: 1536,
                enabled: true,
                params: ParamsMap::new(),
            });
        }
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
    async fn get_returns_404_when_not_configured() {
        let state = state_with_openai(None, false);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/embedding")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn get_returns_view_with_schema() {
        let state = state_with_openai(None, true);
        let app = router(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/embedding")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["provider"], "openai");
        assert_eq!(v["model"], "text-embedding-3-small");
        assert_eq!(v["dimension"], 1536);
        assert!(v["params_schema"].is_object());
    }

    #[tokio::test]
    async fn post_persists_embedding() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        let state = state_with_openai(Some(path.clone()), false);
        let app = router(state.clone());
        let body = json!({
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "enabled": true,
            "params": {}
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["provider"], "openai");
        assert!(state.config.load().embedding.is_some());
        let disk = tokio::fs::read_to_string(&path).await.unwrap();
        assert!(disk.contains("[embedding]"));
    }

    #[tokio::test]
    async fn post_rejects_missing_provider_slot() {
        let tmp = TempDir::new().unwrap();
        let state = state_with_openai(Some(tmp.path().join("config.toml")), false);
        let app = router(state);
        let body = json!({
            "provider": "nope",
            "model": "x",
            "dimension": 1,
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "provider_missing");
    }

    #[tokio::test]
    async fn post_rejects_zero_dimension() {
        let tmp = TempDir::new().unwrap();
        let state = state_with_openai(Some(tmp.path().join("config.toml")), false);
        let app = router(state);
        let body = json!({
            "provider": "openai",
            "model": "x",
            "dimension": 0,
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn benchmark_returns_503_when_sidecar_unreachable() {
        // Point the handler at a port nothing is listening on — reqwest's
        // connect failure maps to 503 `python_sidecar_unavailable` so the
        // UI can distinguish "Python down" from validation errors.
        //
        // We use an invalid hostname (TLD `.invalid` is reserved per RFC
        // 2606) which forces a DNS resolution failure faster + more
        // reliably than probing a closed port on localhost.
        std::env::set_var(
            "CORLINMAN_PY_ADMIN_URL",
            "http://corlinman-unreachable.invalid:50052",
        );
        let state = state_with_openai(None, true);
        let app = router(state);
        let body = json!({
            "samples": ["hello", "world"]
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding/benchmark")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        std::env::remove_var("CORLINMAN_PY_ADMIN_URL");
        let status = resp.status();
        let v = body_json(resp).await;
        assert_eq!(
            status,
            StatusCode::SERVICE_UNAVAILABLE,
            "expected 503 on sidecar unreachable, got {status} body={v}"
        );
        assert_eq!(
            v["error"], "python_sidecar_unavailable",
            "expected python_sidecar_unavailable body, got {v}"
        );
    }

    /// PR-#2 review issue #1: if the in-memory snapshot somehow carries
    /// a sentinel value (e.g. a botched earlier hot-reload), the
    /// embedding POST handler must refuse to persist anything that
    /// still pins `"***REDACTED***"` on disk — the merge step has
    /// nothing real to fall back to, so the belt-and-braces
    /// `has_redacted_sentinel` guard returns 422.
    #[tokio::test]
    async fn post_refuses_when_snapshot_carries_sentinel() {
        use corlinman_core::config::REDACTED_SENTINEL;

        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("config.toml");
        // Seed a snapshot whose admin.password_hash has been left in
        // sentinel state (simulates a buggy upstream that injected the
        // redacted echo into live state). The merge has nothing to
        // restore from because both sides hold the sentinel.
        let mut cfg = Config::default();
        cfg.admin.password_hash = Some(REDACTED_SENTINEL.into());
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
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_config_path(path.clone());
        let app = router(state.clone());

        let body = json!({
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimension": 1536,
            "enabled": true,
            "params": {}
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "redacted_payload");
        // No file on disk — the guard ran before the atomic write.
        assert!(!path.exists());
    }

    #[tokio::test]
    async fn benchmark_rejects_empty_samples() {
        let state = state_with_openai(None, true);
        let app = router(state);
        let body = json!({ "samples": [] });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/embedding/benchmark")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
