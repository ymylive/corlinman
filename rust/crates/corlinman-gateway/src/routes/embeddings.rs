//! `POST /v1/embeddings` — proxy to Python Embedding service via gRPC.
//!
//! Deferred to a later milestone; this stub returns 501 so probes don't panic.

use axum::{routing::post, Router};

use super::not_implemented;

pub fn router() -> Router {
    Router::new().route("/v1/embeddings", post(|| not_implemented("/v1/embeddings")))
}
