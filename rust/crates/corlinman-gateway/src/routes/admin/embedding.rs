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
//!   latency report for a set of sample strings. Proxies to the Python
//!   helper; until the gRPC method is wired up, returns **501
//!   `pending_python_implementation`** so the UI can render a "coming
//!   soon" surface without feature-flagging.

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
    pub limit: Option<usize>,
}

async fn post_benchmark(
    State(_state): State<AdminState>,
    Json(body): Json<BenchmarkBody>,
) -> Response {
    // Minimal sanity check so obvious client bugs surface as 400 rather than
    // 501 (distinguishing pilot-error from "not wired yet").
    if body.samples.is_empty() {
        return bad_request("invalid_samples", "samples must be non-empty");
    }
    if body.samples.len() > 20 {
        return bad_request(
            "too_many_samples",
            "samples is capped at 20 entries per benchmark call",
        );
    }

    // Real implementation proxies to the Python embedding service over gRPC
    // via a new BenchmarkEmbedding RPC. That RPC is being landed by the
    // Python agent in a parallel worktree — until the orchestrator wires
    // them together, advertise the endpoint but return 501 so the UI can
    // show a disabled state without spinning on a timeout.
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({
            "error": "pending_python_implementation",
            "message": "embedding benchmark proxies to the Python embedding service; that gRPC path is not wired yet",
        })),
    )
        .into_response()
}

// ---------------------------------------------------------------------------
// Shared helpers
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
        cfg.providers.openai = Some(ProviderEntry {
            kind: None,
            api_key: Some(SecretRef::EnvVar {
                env: "OPENAI_API_KEY".into(),
            }),
            base_url: None,
            enabled: true,
            params: ParamsMap::new(),
        });
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
    async fn benchmark_returns_501_pending_python() {
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
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "pending_python_implementation");
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
