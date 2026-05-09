//! HTTP route modules mounted by `server::build_router`.
//!
//! Only `health` is fully implemented in this milestone; the other submodules
//! return 501 Not Implemented via [`not_implemented`] so external callers get
//! an honest response while we wire the real handlers in later milestones.

pub mod admin;
pub mod canvas;
pub mod channels;
pub mod chat;
pub mod chat_approve;
pub mod embeddings;
pub mod health;
pub mod metrics;
pub mod models;
pub mod plugin_callback;
pub mod voice;

use std::sync::Arc;

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json, Router,
};
use corlinman_plugins::AsyncTaskRegistry;
use serde_json::json;

pub use health::HealthState;

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
        .merge(metrics::router())
        .merge(voice::router())
}

/// Same as [`router`] but the chat route is backed by the supplied
/// [`chat::ChatState`]. The plugin_callback route still 501s — tests that
/// need the real callback wiring use [`router_with_full_state`].
pub fn router_with_chat_state(state: chat::ChatState) -> Router {
    Router::new()
        .merge(health::router())
        .merge(chat::router_with_state(state.clone()))
        .merge(chat_approve::router_with_state(state))
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router())
        .merge(metrics::router())
        .merge(voice::router())
}

/// Same as [`router_with_chat_state`] but also wires `/plugin-callback/
/// :task_id` onto the supplied [`AsyncTaskRegistry`]. Production boot uses
/// this variant so async plugins can deliver their deferred results.
pub fn router_with_full_state(
    chat_state: chat::ChatState,
    async_tasks: Arc<AsyncTaskRegistry>,
) -> Router {
    Router::new()
        .merge(health::router())
        .merge(chat::router_with_state(chat_state.clone()))
        .merge(chat_approve::router_with_state(chat_state))
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router_with_state(async_tasks))
        .merge(metrics::router())
        .merge(voice::router())
}

/// Same as [`router_with_full_state`] but `/health` is backed by a real
/// [`HealthState`] that runs live probes on every request.
pub fn router_with_full_state_and_health(
    chat_state: chat::ChatState,
    async_tasks: Arc<AsyncTaskRegistry>,
    health_state: HealthState,
) -> Router {
    Router::new()
        .merge(health::router_with_state(health_state))
        .merge(chat::router_with_state(chat_state.clone()))
        .merge(chat_approve::router_with_state(chat_state))
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router_with_state(async_tasks))
        .merge(metrics::router())
        .merge(voice::router())
}

/// Variant of [`router_with_full_state_and_health`] that also wires the
/// live `/voice` route off the shared config snapshot. Production boot
/// uses this when `[voice]` could be hot-flipped on; tests that don't
/// need the route can keep using the plain variant.
pub fn router_with_full_state_and_voice(
    chat_state: chat::ChatState,
    async_tasks: Arc<AsyncTaskRegistry>,
    health_state: HealthState,
    voice_state: voice::VoiceState,
) -> Router {
    Router::new()
        .merge(health::router_with_state(health_state))
        .merge(chat::router_with_state(chat_state))
        .merge(embeddings::router())
        .merge(models::router())
        .merge(admin::router())
        .merge(plugin_callback::router_with_state(async_tasks))
        .merge(metrics::router())
        .merge(voice::router_with_state(voice_state))
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
