//! Adapter that wraps [`corlinman_vector::SqliteStore`] behind the
//! [`MemoryHost`] trait.
//!
//! Reuses the existing BM25 path — we do **not** duplicate SQL and
//! we do not touch the `.usearch` HNSW side. Callers that want dense
//! recall wrap a full `VectorStore` in a different adapter later.
//!
//! ## `query`
//!
//! Delegates to [`SqliteStore::search_bm25`] (or the namespace-filtered
//! variant when `MemoryQuery::namespace` is set), then hydrates chunks
//! via [`SqliteStore::query_chunks_by_ids`]. Structured [`MemoryFilter`]
//! variants are intentionally ignored in this skeleton and logged at
//! `debug!` — filter pushdown is a later-phase integration that will
//! route through [`corlinman_vector::hybrid::TagFilter`].
//!
//! ## `upsert`
//!
//! Writes a synthetic `files` row (path namespaced under
//! `memory-host://`) plus one `chunks` row carrying the document
//! content. Returns the chunk id as a string — that is the id the
//! caller passes to [`MemoryHost::delete`].

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use async_trait::async_trait;
use corlinman_vector::SqliteStore;
use tracing::debug;

use crate::{MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

/// Default diary-name tag recorded on synthetic `files` rows created
/// by [`LocalSqliteHost::upsert`]. Kept stable so downstream tools can
/// filter by it if they need to audit memory-host-originated content.
const DEFAULT_DIARY_NAME: &str = "memory-host";

/// [`MemoryHost`] adapter over [`SqliteStore`].
pub struct LocalSqliteHost {
    name: String,
    store: Arc<SqliteStore>,
    /// Monotonic counter appended to synthetic file paths so repeated
    /// upserts within the same microsecond don't collide on the
    /// `files.path UNIQUE` constraint.
    upsert_counter: AtomicU64,
}

impl LocalSqliteHost {
    /// Construct an adapter with a caller-chosen [`MemoryHost::name`].
    pub fn new(name: impl Into<String>, store: Arc<SqliteStore>) -> Self {
        Self {
            name: name.into(),
            store,
            upsert_counter: AtomicU64::new(0),
        }
    }

    /// Borrow the underlying store (primarily for tests).
    pub fn store(&self) -> &SqliteStore {
        &self.store
    }
}

#[async_trait]
impl MemoryHost for LocalSqliteHost {
    fn name(&self) -> &str {
        &self.name
    }

    async fn query(&self, req: MemoryQuery) -> Result<Vec<MemoryHit>> {
        if !req.filters.is_empty() {
            debug!(
                count = req.filters.len(),
                "LocalSqliteHost ignores structured MemoryFilter variants in the skeleton"
            );
        }
        if req.top_k == 0 || req.text.trim().is_empty() {
            return Ok(Vec::new());
        }

        // Namespace filter pushdown: map to the sqlite whitelist the
        // BM25 path already understands.
        let allowed_ids: Option<Vec<i64>> = match req.namespace.as_deref() {
            Some(ns) => Some(
                self.store
                    .filter_chunk_ids_by_namespace(&[ns.to_string()])
                    .await
                    .context("LocalSqliteHost: namespace filter")?,
            ),
            None => None,
        };

        let hits = self
            .store
            .search_bm25_with_filter(&req.text, req.top_k, allowed_ids.as_deref())
            .await
            .context("LocalSqliteHost: BM25 search")?;

        if hits.is_empty() {
            return Ok(Vec::new());
        }

        let ids: Vec<i64> = hits.iter().map(|(id, _)| *id).collect();
        let chunks = self
            .store
            .query_chunks_by_ids(&ids)
            .await
            .context("LocalSqliteHost: hydrate chunks")?;

        // Re-join score with content in the ranking order SQL gave us.
        let mut by_id = std::collections::HashMap::with_capacity(chunks.len());
        for c in chunks {
            by_id.insert(c.id, c);
        }

        let mut out = Vec::with_capacity(hits.len());
        for (id, score) in hits {
            if let Some(c) = by_id.remove(&id) {
                out.push(MemoryHit {
                    id: id.to_string(),
                    content: c.content,
                    score,
                    source: self.name.clone(),
                    metadata: serde_json::json!({
                        "file_id": c.file_id,
                        "chunk_index": c.chunk_index,
                        "namespace": c.namespace,
                    }),
                });
            }
        }
        Ok(out)
    }

    async fn upsert(&self, doc: MemoryDoc) -> Result<String> {
        let counter = self.upsert_counter.fetch_add(1, Ordering::Relaxed);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or_default();
        let synthetic_path = format!("memory-host://{nanos}-{counter}");

        let file_id = self
            .store
            .insert_file(&synthetic_path, DEFAULT_DIARY_NAME, "", 0, 0)
            .await
            .context("LocalSqliteHost: insert synthetic file row")?;

        let namespace = doc.namespace.as_deref().unwrap_or("general");
        let chunk_id = self
            .store
            .insert_chunk(file_id, 0, &doc.content, None, namespace)
            .await
            .context("LocalSqliteHost: insert chunk")?;

        Ok(chunk_id.to_string())
    }

    async fn delete(&self, id: &str) -> Result<()> {
        let chunk_id: i64 = id
            .parse()
            .with_context(|| format!("LocalSqliteHost: invalid chunk id '{id}'"))?;
        self.store
            .delete_chunk_by_id(chunk_id)
            .await
            .context("LocalSqliteHost: delete chunk")?;
        Ok(())
    }

    async fn get(&self, id: &str) -> Result<Option<MemoryHit>> {
        // Phase 4 W3 C1 (MCP `resources/read` over `corlinman://memory/`):
        // a single-row lookup keyed by the chunk id we returned from
        // `upsert` / `query`.
        let chunk_id: i64 = match id.parse() {
            Ok(n) => n,
            // Well-formed id requirement is on the caller; an unparseable
            // id is "unknown to this host", not a hard error.
            Err(_) => return Ok(None),
        };
        let rows = self
            .store
            .query_chunks_by_ids(&[chunk_id])
            .await
            .context("LocalSqliteHost::get: query chunk by id")?;
        let chunk = match rows.into_iter().next() {
            Some(c) => c,
            None => return Ok(None),
        };
        Ok(Some(MemoryHit {
            id: chunk.id.to_string(),
            content: chunk.content,
            // No relevance score for direct id lookup — caller didn't
            // pose a query. 1.0 is the "fully matched" sentinel.
            score: 1.0,
            source: self.name.clone(),
            metadata: serde_json::json!({
                "file_id": chunk.file_id,
                "chunk_index": chunk.chunk_index,
                "namespace": chunk.namespace,
            }),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    async fn fresh_host() -> (LocalSqliteHost, TempDir) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("kb.sqlite");
        let store = Arc::new(SqliteStore::open(&path).await.unwrap());
        (LocalSqliteHost::new("local-kb", store), tmp)
    }

    #[tokio::test]
    async fn upsert_then_query_roundtrip() {
        let (host, _tmp) = fresh_host().await;
        let id = host
            .upsert(MemoryDoc {
                content: "the lazy fox jumps over dogs".into(),
                metadata: serde_json::json!({"author": "x"}),
                namespace: None,
            })
            .await
            .unwrap();
        assert!(!id.is_empty());

        let hits = host
            .query(MemoryQuery {
                text: "lazy fox".into(),
                top_k: 3,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, id);
        assert_eq!(hits[0].source, "local-kb");
        assert!(hits[0].score > 0.0);
        assert!(hits[0].content.contains("lazy fox"));
    }

    #[tokio::test]
    async fn namespace_filter_scopes_results() {
        let (host, _tmp) = fresh_host().await;
        let id_a = host
            .upsert(MemoryDoc {
                content: "alpha document body".into(),
                metadata: serde_json::Value::Null,
                namespace: Some("diary".into()),
            })
            .await
            .unwrap();
        let _id_b = host
            .upsert(MemoryDoc {
                content: "alpha document body".into(),
                metadata: serde_json::Value::Null,
                namespace: Some("papers".into()),
            })
            .await
            .unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "alpha".into(),
                top_k: 10,
                filters: vec![],
                namespace: Some("diary".into()),
            })
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, id_a);
    }

    #[tokio::test]
    async fn delete_removes_hit() {
        let (host, _tmp) = fresh_host().await;
        let id = host
            .upsert(MemoryDoc {
                content: "ephemeral note".into(),
                metadata: serde_json::Value::Null,
                namespace: None,
            })
            .await
            .unwrap();
        host.delete(&id).await.unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "ephemeral".into(),
                top_k: 5,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();
        assert!(hits.is_empty());
    }

    #[tokio::test]
    async fn get_round_trips_upserted_doc() {
        let (host, _tmp) = fresh_host().await;
        let id = host
            .upsert(MemoryDoc {
                content: "the quick brown fox".into(),
                metadata: serde_json::Value::Null,
                namespace: Some("notes".into()),
            })
            .await
            .unwrap();

        let hit = host
            .get(&id)
            .await
            .unwrap()
            .expect("upserted id must be retrievable");
        assert_eq!(hit.id, id);
        assert_eq!(hit.content, "the quick brown fox");
        assert_eq!(hit.source, "local-kb");
        // Score is the "direct lookup" sentinel.
        assert!((hit.score - 1.0).abs() < f32::EPSILON);
        assert_eq!(hit.metadata["namespace"], "notes");
    }

    #[tokio::test]
    async fn get_unknown_id_returns_none() {
        let (host, _tmp) = fresh_host().await;
        // Numeric but unused id.
        assert!(host.get("999999").await.unwrap().is_none());
        // Non-numeric id maps to "unknown" too (lenient — caller decides
        // whether to surface as an error).
        assert!(host.get("not-a-number").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn empty_query_is_empty_result() {
        let (host, _tmp) = fresh_host().await;
        let hits = host
            .query(MemoryQuery {
                text: "".into(),
                top_k: 3,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();
        assert!(hits.is_empty());
    }
}
