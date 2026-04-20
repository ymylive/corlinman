//! Cross-encoder rerank stage sitting on top of RRF fusion (Sprint 3 T6).
//!
//! # Role in the pipeline
//!
//! [`crate::hybrid::HybridSearcher`] runs dense (HNSW) + sparse (BM25) recall
//! and fuses them with RRF. The fused list is a *candidate set* — typically
//! `top_k * overfetch_multiplier` items — that a cross-encoder can then
//! re-order using full `(query, chunk.content)` pair scoring. Cross-encoders
//! are slower per-pair than bi-encoders but give meaningfully better ordering
//! on the narrow set RRF already shortlisted.
//!
//! # Trait contract
//!
//! A [`Reranker`] takes the fused `hits` and returns a (possibly re-ordered,
//! possibly truncated) slice of size ≤ `top_k`. Scores may be rewritten; the
//! implementation decides whether to preserve RRF scores or replace them
//! with the cross-encoder output. Implementations must be `Send + Sync` so
//! [`HybridSearcher`] can hold them behind `Arc<dyn Reranker>`.
//!
//! # Implementations in this crate
//!
//! - [`NoopReranker`] — passthrough, just truncates to `top_k`. Default;
//!   zero overhead when rerank is disabled.
//! - [`GrpcReranker`] — routes to the Python embedding service's `Rerank`
//!   RPC. The proto is **not yet defined** (this round is Rust-side only),
//!   so the RPC call returns `Err(CorlinmanError::Internal("unimplemented: ..."))`
//!   until the proto + client land. `CorlinmanError` has no dedicated
//!   `Unimplemented` variant today; if one gets added later the stub can
//!   switch over without touching callers.
//!
//! [`HybridSearcher`]: crate::hybrid::HybridSearcher

use std::sync::Arc;

use async_trait::async_trait;

use corlinman_core::error::CorlinmanError;

use crate::hybrid::RagHit;

/// Contract for any post-RRF reranker.
///
/// Implementations should be cheap to `Clone` (wrap heavy state in `Arc`)
/// and safe for concurrent use across request handlers.
#[async_trait]
pub trait Reranker: Send + Sync + std::fmt::Debug {
    /// Re-order `hits` in-place and return the best `top_k`.
    ///
    /// `query` is the user's textual query; the implementation feeds it
    /// together with each `RagHit::content` to a cross-encoder. The
    /// returned vec must have length ≤ `top_k` and must be sorted
    /// best-first.
    async fn rerank(
        &self,
        query: &str,
        hits: Vec<RagHit>,
        top_k: usize,
    ) -> Result<Vec<RagHit>, CorlinmanError>;
}

/// Passthrough reranker. Just truncates to `top_k` without touching order.
///
/// This is the default wired into [`crate::hybrid::HybridSearcher::new`]
/// so existing callers who don't care about rerank keep their current
/// RRF-only behaviour.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoopReranker;

#[async_trait]
impl Reranker for NoopReranker {
    async fn rerank(
        &self,
        _query: &str,
        hits: Vec<RagHit>,
        top_k: usize,
    ) -> Result<Vec<RagHit>, CorlinmanError> {
        Ok(hits.into_iter().take(top_k).collect())
    }
}

/// gRPC-backed reranker that forwards to the Python embedding service.
///
/// # Status — stub
///
/// The `Rerank` RPC is **not yet declared** in `proto/embedding.proto`
/// (kept out of scope this round to avoid cross-agent proto churn). This
/// struct exists so config / bootstrap can select it by name; actually
/// calling [`Reranker::rerank`] returns [`CorlinmanError::Internal`]
/// with an `"unimplemented:"` prefix pointing to the roadmap.
///
/// TODO(M6): once the RPC lands, hold a
/// `corlinman_proto::embedding::embedding_service_client::EmbeddingServiceClient`
/// here and translate `Vec<RagHit>` ⇄ `RerankRequest`/`RerankResponse`.
#[derive(Debug, Clone)]
pub struct GrpcReranker {
    /// Endpoint URL of the Python embedding service
    /// (`http://host:port`). Carried through so bootstrap can validate it
    /// even though the client is not yet wired.
    pub endpoint: String,
    /// Model id to pass through to the Python side
    /// (`BAAI/bge-reranker-v2-m3` or a remote model name).
    pub model: String,
}

impl GrpcReranker {
    /// Construct a stub client. No I/O happens here — the gRPC channel
    /// will be opened on first `rerank` call once the proto lands.
    pub fn new(endpoint: impl Into<String>, model: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            model: model.into(),
        }
    }
}

#[async_trait]
impl Reranker for GrpcReranker {
    async fn rerank(
        &self,
        _query: &str,
        _hits: Vec<RagHit>,
        _top_k: usize,
    ) -> Result<Vec<RagHit>, CorlinmanError> {
        // TODO(M6): open tonic channel to self.endpoint and call Rerank RPC.
        Err(CorlinmanError::Internal(format!(
            "unimplemented: GrpcReranker Rerank RPC not declared in proto yet \
             (endpoint={}, model={})",
            self.endpoint, self.model
        )))
    }
}

/// Convenience alias for the trait object used by [`crate::hybrid::HybridSearcher`].
pub type SharedReranker = Arc<dyn Reranker>;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hybrid::HitSource;

    fn hit(id: i64, score: f32) -> RagHit {
        RagHit {
            chunk_id: id,
            file_id: 1,
            content: format!("chunk-{id}"),
            score,
            source: HitSource::Both,
            path: "test.md".into(),
        }
    }

    #[tokio::test]
    async fn noop_truncates_when_more_hits_than_k() {
        let reranker = NoopReranker;
        let hits = vec![hit(1, 0.9), hit(2, 0.8), hit(3, 0.7), hit(4, 0.6)];
        let out = reranker.rerank("q", hits, 2).await.unwrap();
        assert_eq!(out.len(), 2);
        // Passthrough: order unchanged.
        assert_eq!(out[0].chunk_id, 1);
        assert_eq!(out[1].chunk_id, 2);
    }

    #[tokio::test]
    async fn noop_returns_all_when_fewer_hits_than_k() {
        let reranker = NoopReranker;
        let hits = vec![hit(1, 0.9), hit(2, 0.8)];
        let out = reranker.rerank("q", hits, 10).await.unwrap();
        assert_eq!(out.len(), 2);
    }

    #[tokio::test]
    async fn grpc_stub_returns_unimplemented() {
        let reranker = GrpcReranker::new("http://127.0.0.1:50051", "bge-reranker-v2-m3");
        let err = reranker
            .rerank("q", vec![hit(1, 0.9)], 1)
            .await
            .unwrap_err();
        match err {
            CorlinmanError::Internal(msg) => {
                assert!(
                    msg.starts_with("unimplemented:"),
                    "expected unimplemented-prefixed Internal, got {msg:?}"
                );
            }
            other => panic!("expected Internal, got {other:?}"),
        }
    }
}
