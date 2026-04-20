//! `GET /v1/models` — list model ids known via ModelRedirect + configured providers.
//!
//! Deferred to a later milestone; stub returns 501.

use axum::{routing::get, Router};

use super::not_implemented;

pub fn router() -> Router {
    Router::new().route("/v1/models", get(|| not_implemented("/v1/models")))
}
