//! `{{memory.*}}` resolver backed by the unified [`MemoryHost`] trait.
//!
//! The resolver gives prompt authors one runtime recall surface that can be
//! backed by the local SQLite host today and a vector/hybrid host later.

use std::sync::Arc;

use async_trait::async_trait;
use corlinman_core::placeholder::{DynamicResolver, PlaceholderCtx, PlaceholderError};
use corlinman_memory_host::{MemoryHost, MemoryQuery};

/// Default namespace used by the agent-brain curator sync path.
pub const DEFAULT_MEMORY_NAMESPACE: &str = "agent-brain";

/// Default number of hits rendered for `{{memory.<query>}}`.
pub const DEFAULT_TOP_K: usize = 5;

/// Dynamic resolver for `{{memory.<query text>}}`.
pub struct MemoryResolver {
    host: Arc<dyn MemoryHost>,
    namespace: String,
    top_k: usize,
}

impl std::fmt::Debug for MemoryResolver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MemoryResolver")
            .field("host", &self.host.name())
            .field("namespace", &self.namespace)
            .field("top_k", &self.top_k)
            .finish()
    }
}

impl MemoryResolver {
    pub fn new(host: Arc<dyn MemoryHost>) -> Self {
        Self {
            host,
            namespace: DEFAULT_MEMORY_NAMESPACE.to_string(),
            top_k: DEFAULT_TOP_K,
        }
    }

    #[must_use]
    pub fn with_namespace(mut self, namespace: impl Into<String>) -> Self {
        self.namespace = namespace.into();
        self
    }

    #[must_use]
    pub fn with_top_k(mut self, top_k: usize) -> Self {
        self.top_k = top_k.max(1);
        self
    }

    pub fn into_arc(self) -> Arc<dyn DynamicResolver> {
        Arc::new(self)
    }
}

#[async_trait]
impl DynamicResolver for MemoryResolver {
    async fn resolve(&self, key: &str, _ctx: &PlaceholderCtx) -> Result<String, PlaceholderError> {
        let query = key.trim();
        if query.is_empty() {
            return Ok(String::new());
        }

        let hits = self
            .host
            .query(MemoryQuery {
                text: query.to_string(),
                top_k: self.top_k,
                filters: Vec::new(),
                namespace: Some(self.namespace.clone()),
            })
            .await
            .map_err(|err| PlaceholderError::Resolver {
                namespace: "memory".into(),
                message: err.to_string(),
            })?;

        Ok(render_hits(hits))
    }
}

fn render_hits(hits: Vec<corlinman_memory_host::MemoryHit>) -> String {
    if hits.is_empty() {
        return String::new();
    }
    let mut out = String::new();
    for (idx, hit) in hits.iter().enumerate() {
        if idx > 0 {
            out.push('\n');
        }
        out.push_str("- ");
        out.push_str(hit.content.trim());
        out.push_str(" (");
        out.push_str(&hit.source);
        out.push(':');
        out.push_str(&hit.id);
        out.push(')');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_memory_host::{MemoryDoc, MemoryHit};
    use tokio::sync::Mutex;

    #[derive(Default)]
    struct StubHost {
        queries: Mutex<Vec<MemoryQuery>>,
        hits: Vec<MemoryHit>,
    }

    #[async_trait]
    impl MemoryHost for StubHost {
        fn name(&self) -> &str {
            "stub"
        }

        async fn query(&self, req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>> {
            self.queries.lock().await.push(req);
            Ok(self.hits.clone())
        }

        async fn upsert(&self, _doc: MemoryDoc) -> anyhow::Result<String> {
            anyhow::bail!("not used")
        }

        async fn delete(&self, _id: &str) -> anyhow::Result<()> {
            anyhow::bail!("not used")
        }
    }

    #[tokio::test]
    async fn resolver_queries_agent_brain_namespace_and_renders_hits() {
        let host = Arc::new(StubHost {
            queries: Mutex::new(Vec::new()),
            hits: vec![MemoryHit {
                id: "m1".into(),
                content: "Use PostgreSQL for durable state".into(),
                score: 0.9,
                source: "local-kb".into(),
                metadata: serde_json::Value::Null,
            }],
        });
        let resolver = MemoryResolver::new(host.clone()).with_top_k(3);

        let out = resolver
            .resolve(" durable state ", &PlaceholderCtx::new("s"))
            .await
            .unwrap();

        assert!(out.contains("Use PostgreSQL for durable state"));
        assert!(out.contains("local-kb:m1"));
        let queries = host.queries.lock().await;
        assert_eq!(queries[0].text, "durable state");
        assert_eq!(queries[0].top_k, 3);
        assert_eq!(queries[0].namespace.as_deref(), Some("agent-brain"));
    }

    #[tokio::test]
    async fn empty_query_renders_empty_without_calling_host() {
        let host = Arc::new(StubHost::default());
        let resolver = MemoryResolver::new(host.clone());

        let out = resolver
            .resolve("   ", &PlaceholderCtx::new("s"))
            .await
            .unwrap();

        assert_eq!(out, "");
        assert!(host.queries.lock().await.is_empty());
    }
}
