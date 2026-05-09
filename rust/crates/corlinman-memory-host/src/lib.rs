//! corlinman-memory-host — unified memory-source interface.
//!
//! Defines the [`MemoryHost`] trait so external knowledge sources
//! (Notion, remote Pinecone, enterprise wiki, the native SQLite +
//! usearch store) can plug into hybrid search behind a single
//! contract. Three adapters ship in this crate:
//!
//! - [`local_sqlite::LocalSqliteHost`] wraps the existing
//!   [`corlinman_vector::SqliteStore`] BM25 path.
//! - [`remote_http::RemoteHttpHost`] speaks a minimal JSON protocol
//!   over HTTP to an out-of-process memory service.
//! - [`federation::FederatedMemoryHost`] fans out across a set of
//!   hosts and merges the per-host rankings with Reciprocal Rank
//!   Fusion.
//!
//! This crate is a skeleton: integration into
//! [`corlinman_vector::hybrid`] is deliberately out of scope — that
//! lives in a later phase. No new embeddings provider abstraction is
//! introduced; how a host produces scores is its private detail.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

pub mod federation;
pub mod local_sqlite;
pub mod remote_http;

pub use federation::{FederatedMemoryHost, FusionStrategy};
pub use local_sqlite::LocalSqliteHost;
pub use remote_http::RemoteHttpHost;

/// A pluggable memory source.
///
/// Implementations are `Send + Sync` so they can be shared across
/// tokio tasks via `Arc<dyn MemoryHost>`. Every method is `async` —
/// callers never block the runtime even when a remote host is slow.
#[async_trait]
pub trait MemoryHost: Send + Sync {
    /// Unique identifier, e.g. `"local-kb"`, `"notion"`, `"remote-v1"`.
    ///
    /// Returned on every [`MemoryHit`] so downstream code (and the UI)
    /// can attribute a hit to its originating host.
    fn name(&self) -> &str;

    /// Query top-k semantically relevant hits.
    async fn query(&self, req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>>;

    /// Upsert a document; returns the host-assigned id.
    async fn upsert(&self, doc: MemoryDoc) -> anyhow::Result<String>;

    /// Delete by id.
    async fn delete(&self, id: &str) -> anyhow::Result<()>;

    /// Fetch a single document by id. Returns `Ok(None)` when the id
    /// is well-formed but unknown to this host; returns `Err(...)` on
    /// transport / decode failures only.
    ///
    /// Default impl returns `Err(anyhow!("get not supported"))` so
    /// adapters that don't yet implement read-by-id continue compiling.
    /// The Phase 4 W3 C1 (MCP) `resources/read` flow needs this hook;
    /// `local_sqlite::LocalSqliteHost` overrides it (federated +
    /// remote-http inherit the default until they grow a real impl).
    async fn get(&self, _id: &str) -> anyhow::Result<Option<MemoryHit>> {
        anyhow::bail!("MemoryHost::get is not implemented for this adapter")
    }

    /// Optional: health check for observability.
    async fn health(&self) -> HealthStatus {
        HealthStatus::Ok
    }
}

/// Query into a memory host.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryQuery {
    pub text: String,
    pub top_k: usize,
    #[serde(default)]
    pub filters: Vec<MemoryFilter>,
    #[serde(default)]
    pub namespace: Option<String>,
}

/// A single hit returned by a memory host.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryHit {
    pub id: String,
    pub content: String,
    pub score: f32,
    /// Set to the originating host's [`MemoryHost::name`].
    pub source: String,
    #[serde(default)]
    pub metadata: serde_json::Value,
}

/// A document to upsert into a memory host.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryDoc {
    pub content: String,
    #[serde(default)]
    pub metadata: serde_json::Value,
    #[serde(default)]
    pub namespace: Option<String>,
}

/// Structured filter predicate pushed down to a host.
///
/// The enum is marked `non_exhaustive` so we can add new variants
/// without breaking existing hosts — adapters must match exhaustively
/// on the three initial variants and treat unknown future variants as
/// "no-op / log and skip".
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[non_exhaustive]
pub enum MemoryFilter {
    TagEq { tag: String, value: String },
    TagIn { tag: String, values: Vec<String> },
    CreatedAfter { unix: i64 },
}

/// Lightweight health signal surfaced by [`MemoryHost::health`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HealthStatus {
    Ok,
    Degraded(String),
    Down(String),
}

#[cfg(test)]
mod type_tests {
    use super::*;

    #[test]
    fn memory_filter_serde_snake_case() {
        let f = MemoryFilter::TagEq {
            tag: "kind".into(),
            value: "note".into(),
        };
        let s = serde_json::to_string(&f).unwrap();
        assert!(s.contains(r#""kind":"tag_eq""#), "got: {s}");
    }

    #[test]
    fn memory_query_default_fields() {
        let raw = r#"{"text":"hi","top_k":3}"#;
        let q: MemoryQuery = serde_json::from_str(raw).unwrap();
        assert_eq!(q.text, "hi");
        assert_eq!(q.top_k, 3);
        assert!(q.filters.is_empty());
        assert!(q.namespace.is_none());
    }
}
