//! Read-only adapter wrapping any `Arc<dyn MemoryHost>`.
//!
//! Subagents (Phase 4 W4 D3) inherit their parent's `memory_host` so the
//! child can search the same knowledge sources, but every D3 design decision
//! says they MUST NOT mutate that store: a delegated child that learns
//! something new bubbles its findings up to the parent's context, never
//! into shared memory directly. A child that wrote freely could also smuggle
//! state across siblings, defeating the "fresh persona" guarantee.
//!
//! This adapter implements `MemoryHost` by forwarding `query` + `health`
//! to the inner host while rejecting `upsert` / `delete` with a tagged
//! error — callers can branch on the message string to surface
//! "subagent attempted write" telemetry rather than confusing the user
//! with a generic backend failure.
//!
//! See `docs/design/phase4-w4-d3-design.md` § "Memory-host federation
//! contract" for the contract this implements.
//!
//! Composes cleanly with [`crate::FederatedMemoryHost`]: a federated host
//! wrapped read-only continues to fan-out queries across its members and
//! merge with the same Reciprocal Rank Fusion logic; only writes are
//! refused at the outer wrapper before reaching any inner adapter.

use std::sync::Arc;

use async_trait::async_trait;

use crate::{HealthStatus, MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

/// Error message tag returned by `upsert` / `delete` so callers can
/// distinguish a read-only-rejection from a genuine backend failure
/// without parsing English prose.
pub const READ_ONLY_REJECT_TAG: &str = "memory_host_read_only";

/// Wraps any `Arc<dyn MemoryHost>` and forbids `upsert` / `delete`.
///
/// `name()` prefixes the inner host's name with `"ro:"` so attribution
/// in `MemoryHit::source` stays correct without colliding with a
/// hypothetical sibling host that happens to share the inner's name.
pub struct ReadOnlyMemoryHost {
    inner: Arc<dyn MemoryHost>,
    /// Cached `format!("ro:{inner_name}")` so `name()` can return `&str`
    /// without allocating per call.
    cached_name: String,
}

impl ReadOnlyMemoryHost {
    pub fn new(inner: Arc<dyn MemoryHost>) -> Self {
        let cached_name = format!("ro:{}", inner.name());
        Self { inner, cached_name }
    }
}

#[async_trait]
impl MemoryHost for ReadOnlyMemoryHost {
    fn name(&self) -> &str {
        &self.cached_name
    }

    async fn query(&self, req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>> {
        self.inner.query(req).await
    }

    async fn upsert(&self, _doc: MemoryDoc) -> anyhow::Result<String> {
        Err(anyhow::anyhow!(
            "{READ_ONLY_REJECT_TAG}: upsert refused — host '{}' is wrapped read-only \
             (subagent / inherited contexts cannot mutate parent memory)",
            self.inner.name(),
        ))
    }

    async fn delete(&self, id: &str) -> anyhow::Result<()> {
        Err(anyhow::anyhow!(
            "{READ_ONLY_REJECT_TAG}: delete({id}) refused — host '{}' is wrapped read-only",
            self.inner.name(),
        ))
    }

    async fn health(&self) -> HealthStatus {
        self.inner.health().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        federation::{FederatedMemoryHost, FusionStrategy},
        MemoryFilter,
    };

    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Test double: counts how many times each method was invoked + carries
    /// a canned `query` response so we can verify forwarding without a real
    /// backend. Sufficient for trait-level wiring tests.
    struct CountingHost {
        name: String,
        query_calls: AtomicUsize,
        upsert_calls: AtomicUsize,
        delete_calls: AtomicUsize,
        canned_hits: Vec<MemoryHit>,
    }

    impl CountingHost {
        fn with_hits(name: &str, hits: Vec<MemoryHit>) -> Arc<Self> {
            Arc::new(Self {
                name: name.into(),
                query_calls: AtomicUsize::new(0),
                upsert_calls: AtomicUsize::new(0),
                delete_calls: AtomicUsize::new(0),
                canned_hits: hits,
            })
        }
    }

    #[async_trait]
    impl MemoryHost for CountingHost {
        fn name(&self) -> &str {
            &self.name
        }

        async fn query(&self, _req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>> {
            self.query_calls.fetch_add(1, Ordering::SeqCst);
            Ok(self.canned_hits.clone())
        }

        async fn upsert(&self, _doc: MemoryDoc) -> anyhow::Result<String> {
            self.upsert_calls.fetch_add(1, Ordering::SeqCst);
            Ok("inner-id".into())
        }

        async fn delete(&self, _id: &str) -> anyhow::Result<()> {
            self.delete_calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    fn hit(id: &str, source: &str, score: f32) -> MemoryHit {
        MemoryHit {
            id: id.into(),
            content: format!("body of {id}"),
            score,
            source: source.into(),
            metadata: serde_json::Value::Null,
        }
    }

    fn query(text: &str) -> MemoryQuery {
        MemoryQuery {
            text: text.into(),
            top_k: 5,
            filters: vec![] as Vec<MemoryFilter>,
            namespace: None,
        }
    }

    #[tokio::test]
    async fn name_is_prefixed_with_ro() {
        let inner = CountingHost::with_hits("local-kb", vec![]);
        let ro = ReadOnlyMemoryHost::new(inner);
        assert_eq!(ro.name(), "ro:local-kb");
    }

    #[tokio::test]
    async fn query_forwards_to_inner_and_returns_hits() {
        let canned = vec![hit("doc-1", "local-kb", 0.9)];
        let inner = CountingHost::with_hits("local-kb", canned.clone());
        let ro = ReadOnlyMemoryHost::new(inner.clone() as Arc<dyn MemoryHost>);

        let hits = ro.query(query("anything")).await.unwrap();

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "doc-1");
        assert_eq!(inner.query_calls.load(Ordering::SeqCst), 1);
        assert_eq!(inner.upsert_calls.load(Ordering::SeqCst), 0);
        assert_eq!(inner.delete_calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn upsert_returns_tagged_error_and_does_not_call_inner() {
        let inner = CountingHost::with_hits("local-kb", vec![]);
        let ro = ReadOnlyMemoryHost::new(inner.clone() as Arc<dyn MemoryHost>);

        let err = ro
            .upsert(MemoryDoc {
                content: "hello".into(),
                metadata: serde_json::Value::Null,
                namespace: None,
            })
            .await
            .unwrap_err();

        assert!(err.to_string().contains(READ_ONLY_REJECT_TAG));
        assert!(err.to_string().contains("local-kb"));
        assert_eq!(inner.upsert_calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn delete_returns_tagged_error_and_does_not_call_inner() {
        let inner = CountingHost::with_hits("local-kb", vec![]);
        let ro = ReadOnlyMemoryHost::new(inner.clone() as Arc<dyn MemoryHost>);

        let err = ro.delete("doc-1").await.unwrap_err();

        assert!(err.to_string().contains(READ_ONLY_REJECT_TAG));
        assert!(err.to_string().contains("doc-1"));
        assert_eq!(inner.delete_calls.load(Ordering::SeqCst), 0);
    }

    /// RRF roundtrip: wrapping a `FederatedMemoryHost` read-only must
    /// preserve the fan-out + fusion behaviour for queries; only writes
    /// get the tagged-rejection treatment.
    #[tokio::test]
    async fn federated_host_wrapped_readonly_still_does_rrf() {
        let host_a: Arc<dyn MemoryHost> = CountingHost::with_hits(
            "kb-a",
            vec![hit("a-1", "kb-a", 1.0), hit("a-2", "kb-a", 0.7)],
        );
        let host_b: Arc<dyn MemoryHost> = CountingHost::with_hits(
            "kb-b",
            vec![hit("b-1", "kb-b", 0.95), hit("a-1", "kb-b", 0.4)],
        );

        let federated =
            FederatedMemoryHost::new("fed", vec![host_a, host_b], FusionStrategy::Rrf { k: 60.0 });
        let ro = ReadOnlyMemoryHost::new(Arc::new(federated));

        let hits = ro.query(query("anything")).await.unwrap();

        // Query path: same merging behaviour as the underlying federated
        // host. Document `a-1` appears in both hosts so RRF should rank it
        // ahead of `a-2` (only in host A) despite a-1's lower per-host score
        // in B.
        assert!(
            !hits.is_empty(),
            "RRF should produce hits through the wrapper"
        );
        let ids: Vec<&str> = hits.iter().map(|h| h.id.as_str()).collect();
        assert!(ids.contains(&"a-1"));

        // Write path: refused regardless of nested host shape.
        let err = ro
            .upsert(MemoryDoc {
                content: "shouldn't reach federation".into(),
                metadata: serde_json::Value::Null,
                namespace: None,
            })
            .await
            .unwrap_err();
        assert!(err.to_string().contains(READ_ONLY_REJECT_TAG));
    }
}
