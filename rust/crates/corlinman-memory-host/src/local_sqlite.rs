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
use serde_json::Value;
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

        self.store
            .ensure_memory_host_metadata_schema()
            .await
            .context("LocalSqliteHost: ensure metadata schema")?;

        let hits = self
            .store
            .search_bm25_with_filter(&req.text, req.top_k, allowed_ids.as_deref())
            .await
            .context("LocalSqliteHost: BM25 search")?;

        if hits.is_empty() {
            return Ok(Vec::new());
        }

        let mut scored: Vec<(i64, f32, bool)> = hits
            .iter()
            .map(|(id, score)| (*id, *score, false))
            .collect();
        let seed_ids: Vec<i64> = hits.iter().map(|(id, _)| *id).collect();
        let expanded_ids = self
            .one_hop_graph_ids(&seed_ids, req.namespace.as_deref())
            .await?;
        let mut seen: std::collections::HashSet<i64> = seed_ids.iter().copied().collect();
        let seed_floor = hits.iter().map(|(_, score)| *score).fold(0.0_f32, f32::max) * 0.85;
        for id in expanded_ids {
            if seen.insert(id) {
                scored.push((id, seed_floor, true));
            }
        }
        let candidate_ids: Vec<i64> = scored.iter().map(|(id, _, _)| *id).collect();
        let metadata_by_id = self
            .metadata_for_chunk_ids(&candidate_ids)
            .await
            .context("LocalSqliteHost: hydrate metadata")?;

        let mut budgeted = Vec::with_capacity(req.top_k);
        let mut seen_node_ids = std::collections::HashSet::new();
        for (id, score, graph_expanded) in scored {
            if let Some(metadata) = metadata_by_id.get(&id) {
                if let Some(node_id) = metadata.get("node_id").and_then(Value::as_str) {
                    if !seen_node_ids.insert(node_id.to_string()) {
                        continue;
                    }
                }
            }
            budgeted.push((id, score, graph_expanded));
            if budgeted.len() >= req.top_k {
                break;
            }
        }

        let ids: Vec<i64> = budgeted.iter().map(|(id, _, _)| *id).collect();
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

        let mut out = Vec::with_capacity(budgeted.len());
        for (id, score, graph_expanded) in budgeted {
            if let Some(c) = by_id.remove(&id) {
                let metadata = merge_metadata(
                    serde_json::json!({
                        "file_id": c.file_id,
                        "chunk_index": c.chunk_index,
                        "namespace": c.namespace,
                        "graph_expanded": graph_expanded,
                    }),
                    metadata_by_id.get(&id),
                );
                out.push(MemoryHit {
                    id: id.to_string(),
                    content: c.content,
                    score,
                    source: self.name.clone(),
                    metadata,
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

        self.store
            .ensure_memory_host_metadata_schema()
            .await
            .context("LocalSqliteHost: ensure metadata schema")?;
        self.upsert_metadata(chunk_id, namespace, &doc.metadata)
            .await?;

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
        self.store
            .ensure_memory_host_metadata_schema()
            .await
            .context("LocalSqliteHost: ensure metadata schema")?;
        let metadata_by_id = self.metadata_for_chunk_ids(&[chunk_id]).await?;
        let metadata = merge_metadata(
            serde_json::json!({
                "file_id": chunk.file_id,
                "chunk_index": chunk.chunk_index,
                "namespace": chunk.namespace,
            }),
            metadata_by_id.get(&chunk_id),
        );
        Ok(Some(MemoryHit {
            id: chunk.id.to_string(),
            content: chunk.content,
            // No relevance score for direct id lookup — caller didn't
            // pose a query. 1.0 is the "fully matched" sentinel.
            score: 1.0,
            source: self.name.clone(),
            metadata,
        }))
    }
}

impl LocalSqliteHost {
    async fn upsert_metadata(
        &self,
        chunk_id: i64,
        namespace: &str,
        metadata: &Value,
    ) -> Result<()> {
        let node_id = metadata
            .get("node_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        self.store
            .upsert_memory_host_metadata(
                chunk_id,
                namespace,
                &metadata.to_string(),
                node_id.as_deref(),
            )
            .await
            .context("LocalSqliteHost: upsert metadata")?;
        Ok(())
    }

    async fn metadata_for_chunk_ids(
        &self,
        chunk_ids: &[i64],
    ) -> Result<std::collections::HashMap<i64, Value>> {
        if chunk_ids.is_empty() {
            return Ok(std::collections::HashMap::new());
        }
        let rows = self
            .store
            .memory_host_metadata_by_chunk_ids(chunk_ids)
            .await
            .context("LocalSqliteHost: query metadata by chunk ids")?;
        let mut out = std::collections::HashMap::with_capacity(rows.len());
        for row in rows {
            let value = serde_json::from_str(&row.metadata).unwrap_or(Value::Null);
            out.insert(row.chunk_id, value);
        }
        Ok(out)
    }

    async fn one_hop_graph_ids(
        &self,
        seed_chunk_ids: &[i64],
        namespace: Option<&str>,
    ) -> Result<Vec<i64>> {
        if seed_chunk_ids.is_empty() {
            return Ok(Vec::new());
        }
        let seed_metadata = self.metadata_for_chunk_ids(seed_chunk_ids).await?;
        let mut seed_node_ids = Vec::new();
        let mut linked_node_ids = Vec::new();
        for metadata in seed_metadata.values() {
            if let Some(node_id) = metadata.get("node_id").and_then(Value::as_str) {
                seed_node_ids.push(node_id.to_string());
            }
            linked_node_ids.extend(json_string_array(metadata.get("links")));
        }
        let mut wanted = Vec::new();
        wanted.extend(linked_node_ids);
        wanted.extend(
            self.backlinked_node_ids(&seed_node_ids, namespace)
                .await
                .context("LocalSqliteHost: query backlinks")?,
        );
        wanted = dedupe_strings(wanted);
        if wanted.is_empty() {
            return Ok(Vec::new());
        }
        self.chunk_ids_for_node_ids(&wanted, namespace).await
    }

    async fn backlinked_node_ids(
        &self,
        seed_node_ids: &[String],
        namespace: Option<&str>,
    ) -> Result<Vec<String>> {
        if seed_node_ids.is_empty() {
            return Ok(Vec::new());
        }
        let rows = self
            .store
            .list_memory_host_metadata(namespace)
            .await
            .context("LocalSqliteHost: scan graph metadata")?;
        let seed: std::collections::HashSet<&str> =
            seed_node_ids.iter().map(String::as_str).collect();
        let mut out = Vec::new();
        for row in rows {
            let metadata: Value = serde_json::from_str(&row.metadata).unwrap_or(Value::Null);
            let links = json_string_array(metadata.get("links"));
            if links.iter().any(|link| seed.contains(link.as_str())) {
                if let Some(node_id) = row.node_id {
                    out.push(node_id);
                }
            }
        }
        Ok(dedupe_strings(out))
    }

    async fn chunk_ids_for_node_ids(
        &self,
        node_ids: &[String],
        namespace: Option<&str>,
    ) -> Result<Vec<i64>> {
        if node_ids.is_empty() {
            return Ok(Vec::new());
        }
        self.store
            .memory_host_chunk_ids_by_node_ids(node_ids, namespace)
            .await
            .context("LocalSqliteHost: query one-hop chunks")
    }
}

fn merge_metadata(base: Value, stored: Option<&Value>) -> Value {
    let mut base_obj = serde_json::Map::new();
    if let Some(Value::Object(stored_obj)) = stored {
        for (key, value) in stored_obj {
            base_obj.insert(key.clone(), value.clone());
        }
    }
    if let Value::Object(host_obj) = base {
        for (key, value) in host_obj {
            base_obj.insert(key, value);
        }
    }
    Value::Object(base_obj)
}

fn json_string_array(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    }
}

fn dedupe_strings(items: Vec<String>) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for item in items {
        if seen.insert(item.clone()) {
            out.push(item);
        }
    }
    out
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
    async fn query_preserves_upserted_metadata() {
        let (host, _tmp) = fresh_host().await;
        host.upsert(MemoryDoc {
            content: "alpha graph node".into(),
            metadata: serde_json::json!({
                "node_id": "kn-a",
                "title": "Alpha Node",
                "links": ["kn-b"],
                "related_nodes": ["Beta Node"]
            }),
            namespace: Some("agent-brain".into()),
        })
        .await
        .unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "alpha".into(),
                top_k: 3,
                filters: vec![],
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].metadata["node_id"], "kn-a");
        assert_eq!(hits[0].metadata["title"], "Alpha Node");
        assert_eq!(hits[0].metadata["links"], serde_json::json!(["kn-b"]));
        assert_eq!(
            hits[0].metadata["related_nodes"],
            serde_json::json!(["Beta Node"])
        );
    }

    #[tokio::test]
    async fn query_expands_one_hop_links_after_bm25_seed() {
        let (host, _tmp) = fresh_host().await;
        let id_a = host
            .upsert(MemoryDoc {
                content: "alpha seed memory".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-a",
                    "title": "Alpha",
                    "links": ["kn-b"]
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();
        let id_b = host
            .upsert(MemoryDoc {
                content: "beta linked context without query term".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-b",
                    "title": "Beta",
                    "links": []
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();
        let id_c = host
            .upsert(MemoryDoc {
                content: "gamma backlink context without query term".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-c",
                    "title": "Gamma",
                    "links": ["kn-a"]
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "alpha".into(),
                top_k: 3,
                filters: vec![],
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        let ids: Vec<&str> = hits.iter().map(|h| h.id.as_str()).collect();
        assert_eq!(ids, vec![id_a.as_str(), id_b.as_str(), id_c.as_str()]);
        assert_eq!(hits[0].metadata["graph_expanded"], false);
        assert_eq!(hits[1].metadata["graph_expanded"], true);
        assert_eq!(hits[2].metadata["graph_expanded"], true);
    }

    #[tokio::test]
    async fn query_dedupes_by_node_id_and_host_metadata_wins() {
        let (host, _tmp) = fresh_host().await;
        let id_a = host
            .upsert(MemoryDoc {
                content: "alpha duplicate first".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-a",
                    "title": "Alpha",
                    "namespace": "spoofed",
                    "graph_expanded": true
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();
        let _id_dup = host
            .upsert(MemoryDoc {
                content: "alpha duplicate second".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-a",
                    "title": "Alpha duplicate"
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "alpha duplicate".into(),
                top_k: 5,
                filters: vec![],
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, id_a);
        assert_eq!(hits[0].metadata["namespace"], "agent-brain");
        assert_eq!(hits[0].metadata["graph_expanded"], false);
    }

    #[tokio::test]
    async fn query_dedupes_before_applying_top_k_budget() {
        let (host, _tmp) = fresh_host().await;
        let id_a = host
            .upsert(MemoryDoc {
                content: "alpha duplicate first".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-a",
                    "title": "Alpha",
                    "links": ["kn-b"]
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();
        let _id_dup = host
            .upsert(MemoryDoc {
                content: "alpha duplicate second".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-a",
                    "title": "Alpha duplicate",
                    "links": []
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();
        let id_b = host
            .upsert(MemoryDoc {
                content: "beta linked context without query term".into(),
                metadata: serde_json::json!({
                    "node_id": "kn-b",
                    "title": "Beta",
                    "links": []
                }),
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        let hits = host
            .query(MemoryQuery {
                text: "alpha duplicate".into(),
                top_k: 2,
                filters: vec![],
                namespace: Some("agent-brain".into()),
            })
            .await
            .unwrap();

        let ids: Vec<&str> = hits.iter().map(|h| h.id.as_str()).collect();
        assert_eq!(ids, vec![id_a.as_str(), id_b.as_str()]);
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
