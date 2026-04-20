//! `GET/POST /plugin-callback` — asynchronous plugin completion webhook.
//!
//! Deferred to a later milestone; stub returns 501.

use axum::{routing::any, Router};

use super::not_implemented;

pub fn router() -> Router {
    Router::new().route(
        "/plugin-callback",
        any(|| not_implemented("/plugin-callback")),
    )
}
