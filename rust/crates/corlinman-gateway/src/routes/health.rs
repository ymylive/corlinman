//! `GET /health` — lightweight liveness probe.
//!
//! The full contract (plan §9) enumerates checks for config, python-agent-grpc,
//! sqlite, usearch, plugin-registry, and channels.qq. Those checks land in
//! later milestones; this shim returns the final shape with an empty `checks`
//! array so external probes can already bind to the route.

use axum::{routing::get, Json, Router};
use serde::Serialize;

const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub version: &'static str,
    pub checks: Vec<CheckEntry>,
}

#[derive(Serialize)]
pub struct CheckEntry {
    pub name: String,
    pub status: String,
    pub detail: Option<String>,
}

/// Axum router exposing `GET /health`.
pub fn router() -> Router {
    Router::new().route("/health", get(health))
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        version: VERSION,
        checks: Vec::new(),
    })
}
