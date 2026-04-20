//! `/admin/*` REST + SSE endpoints for the Next.js UI.
//!
//! All admin routes land in a later milestone (plan §7). For now we expose a
//! single catch-all that answers 501 so the UI can distinguish "gateway up but
//! admin not ready" from "gateway down".

use axum::{routing::any, Router};

use super::not_implemented;

pub fn router() -> Router {
    Router::new().route("/admin/*path", any(|| not_implemented("/admin/*")))
}
