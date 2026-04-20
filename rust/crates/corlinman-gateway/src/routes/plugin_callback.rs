//! `POST /plugin-callback/:task_id` — async plugin completion webhook.
//!
//! Long-running ("async") plugins respond to `tools/call` with a synthetic
//! `{"task_id": "tsk_..."}` result, which the gateway's `RegistryToolExecutor`
//! parks on the process-wide [`AsyncTaskRegistry`]. The plugin later POSTs the
//! real result here; that call wakes the parked tool call and the chat
//! reasoning loop resumes.
//!
//! Auth model: the `task_id` itself is a one-shot, unguessable credential —
//! the registry only accepts the first callback for a given id and drops the
//! entry. No other authentication sits in front of this route so plugins
//! running on the same host can reach it without shared secrets. Task ids are
//! produced by plugins (assumed high-entropy per §3 roadmap) and are never
//! logged in cleartext outside debug scopes.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::post,
    Json, Router,
};
use corlinman_plugins::{AsyncTaskRegistry, CompleteError};
use serde_json::{json, Value};

/// Handler wiring: matches the path segment `:task_id` and a JSON body
/// containing whatever payload the plugin wants to surface back to the model.
/// The payload is passed through to the parked waiter verbatim.
pub async fn plugin_callback(
    State(registry): State<Arc<AsyncTaskRegistry>>,
    Path(task_id): Path<String>,
    Json(payload): Json<Value>,
) -> impl IntoResponse {
    match registry.complete(&task_id, payload) {
        Ok(()) => (StatusCode::OK, Json(json!({"status": "ok"}))).into_response(),
        Err(CompleteError::NotFound) => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "task_not_found",
                "task_id": task_id,
            })),
        )
            .into_response(),
        Err(CompleteError::WaiterDropped) => (
            StatusCode::GONE,
            Json(json!({
                "error": "waiter_dropped",
                "task_id": task_id,
                "message": "callback arrived after chat client disconnected or timed out",
            })),
        )
            .into_response(),
    }
}

/// Build a router keyed on `Arc<AsyncTaskRegistry>`. Callers in `server.rs`
/// pull the registry out of the loaded `PluginRegistry` and supply it here.
pub fn router_with_state(registry: Arc<AsyncTaskRegistry>) -> Router {
    Router::new()
        .route("/plugin-callback/:task_id", post(plugin_callback))
        .with_state(registry)
}

/// Stub router used when no `PluginRegistry` has been wired (test fixtures /
/// boot-before-discovery). Returns 501 so callers get an honest signal.
pub fn router() -> Router {
    Router::new().route(
        "/plugin-callback/:task_id",
        post(|| async {
            (
                StatusCode::NOT_IMPLEMENTED,
                Json(json!({
                    "error": "not_implemented",
                    "route": "/plugin-callback/:task_id",
                    "message": "no AsyncTaskRegistry wired; build router via router_with_state()",
                })),
            )
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use tower::ServiceExt;

    fn app(registry: Arc<AsyncTaskRegistry>) -> Router {
        router_with_state(registry)
    }

    #[tokio::test]
    async fn callback_completes_pending_task_and_returns_ok() {
        let registry = Arc::new(AsyncTaskRegistry::new());
        let rx = registry.register("tsk_ok".into());
        let app = app(registry);

        let req = Request::builder()
            .method("POST")
            .uri("/plugin-callback/tsk_ok")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({"value": 42})).unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // The waiter must have received the exact payload.
        let payload = rx.await.expect("waiter receives payload");
        assert_eq!(payload["value"], 42);
    }

    #[tokio::test]
    async fn callback_for_unknown_task_returns_404() {
        let registry = Arc::new(AsyncTaskRegistry::new());
        let app = app(registry);

        let req = Request::builder()
            .method("POST")
            .uri("/plugin-callback/tsk_missing")
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&json!({})).unwrap()))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"], "task_not_found");
    }

    #[tokio::test]
    async fn callback_after_waiter_dropped_returns_410() {
        let registry = Arc::new(AsyncTaskRegistry::new());
        let rx = registry.register("tsk_drop".into());
        drop(rx);
        let app = app(registry);

        let req = Request::builder()
            .method("POST")
            .uri("/plugin-callback/tsk_drop")
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&json!({})).unwrap()))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::GONE);
    }
}
