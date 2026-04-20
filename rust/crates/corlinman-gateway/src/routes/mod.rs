//! HTTP route modules mounted by `server::build_router`.
//!
//! Only `health` is fully implemented in this milestone; the other submodules
//! return 501 Not Implemented via [`not_implemented`] so external callers get
//! an honest response while we wire the real handlers in later milestones.

pub mod admin;
pub mod chat;
pub mod embeddings;
pub mod health;
pub mod models;
pub mod plugin_callback;

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json, Router,
};
use serde_json::json;

/// Compose every route submodule into a single router. `chat` uses its 501
/// stub; callers that have a `ChatBackend` should use
/// [`router_with_chat_state`] instead.
pub fn router() -> Router {
    Router::new()
        .merge(health::router())
        .merge(chat::router())
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router())
}

/// Same as [`router`] but the chat route is backed by the supplied
/// [`chat::ChatState`].
pub fn router_with_chat_state(state: chat::ChatState) -> Router {
    Router::new()
        .merge(health::router())
        .merge(chat::router_with_state(state))
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router())
}

/// Placeholder handler for routes whose behaviour lands in a later milestone.
pub async fn not_implemented(route: &'static str) -> Response {
    (
        StatusCode::NOT_IMPLEMENTED,
        Json(json!({
            "error": "not_implemented",
            "route": route,
            "message": "handler not wired yet; see corlinman plan §2",
        })),
    )
        .into_response()
}
