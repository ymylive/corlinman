//! Public facade combining [`SqliteStore`] and [`UsearchIndex`] into a
//! single hybrid-search entry point.
//!
//! # Pipeline
//!
//! - `VectorStore::query` delegates to [`HybridSearcher::search`] (HNSW
//!   + BM25 + RRF fusion).
//! - `VectorStore::query_dense` and `VectorStore::query_sparse` expose
//!   single-path fallbacks for A/B comparisons and graceful
//!   degradation when one recall path is unavailable.
//!
//! # Not yet implemented
//!
//! - Cross-encoder rerank (M6).
//! - LRU unload of `.usearch` files on idle timeout.
//! - Tag / metadata filter pushdown.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use tokio::sync::RwLock;

use crate::hybrid::{HybridParams, HybridSearcher, RagHit};
use crate::sqlite::SqliteStore;
use crate::usearch_index::UsearchIndex;

/// A SQLite chunk store + one loaded usearch index, wired through
/// [`HybridSearcher`].
pub struct VectorStore {
    sqlite: Arc<SqliteStore>,
    index: Arc<RwLock<UsearchIndex>>,
    hybrid: HybridSearcher,
}

impl VectorStore {
    /// Open a SQLite file + its associated `.usearch` file.
    ///
    /// Both paths are validated eagerly; dimension parity is the
    /// caller's responsibility (use
    /// [`UsearchIndex::open_checked`] upstream if you know the target
    /// model's dim).
    pub async fn open(sqlite_path: &Path, usearch_path: &Path) -> Result<Self> {
        let sqlite = SqliteStore::open(sqlite_path)
            .await
            .with_context(|| format!("open sqlite '{}'", sqlite_path.display()))?;
        let index = UsearchIndex::open(usearch_path)
            .with_context(|| format!("open usearch '{}'", usearch_path.display()))?;
        Ok(Self::from_parts(sqlite, index))
    }

    /// Construct from already-opened components (primarily for tests +
    /// cases where the caller needs [`UsearchIndex::open_checked`]).
    pub fn from_parts(sqlite: SqliteStore, index: UsearchIndex) -> Self {
        Self::from_parts_with_params(sqlite, index, HybridParams::default())
    }

    /// Same as [`Self::from_parts`] but lets callers override the
    /// default [`HybridParams`].
    pub fn from_parts_with_params(
        sqlite: SqliteStore,
        index: UsearchIndex,
        params: HybridParams,
    ) -> Self {
        let sqlite = Arc::new(sqlite);
        let index = Arc::new(RwLock::new(index));
        let hybrid = HybridSearcher::new(sqlite.clone(), index.clone(), params);
        Self {
            sqlite,
            index,
            hybrid,
        }
    }

    /// Borrow the SQLite store (read-side helpers).
    pub fn sqlite(&self) -> &SqliteStore {
        &self.sqlite
    }

    /// Shared handle to the usearch index (exposes read / write locks).
    pub fn index(&self) -> &Arc<RwLock<UsearchIndex>> {
        &self.index
    }

    /// Hybrid query — HNSW + BM25 + RRF fusion, `top_k` final results.
    ///
    /// `query_text` drives BM25; `query_vector` drives HNSW. Pass an
    /// empty `query_text` or empty `query_vector` to implicitly
    /// reduce to the other path.
    pub async fn query(
        &self,
        query_text: &str,
        query_vector: &[f32],
        top_k: usize,
    ) -> Result<Vec<RagHit>> {
        let overrides = HybridParams {
            top_k,
            ..self.hybrid.params()
        };
        self.hybrid
            .search(query_text, query_vector, Some(overrides))
            .await
    }

    /// Dense-only fallback (HNSW).
    pub async fn query_dense(&self, query_vector: &[f32], top_k: usize) -> Result<Vec<RagHit>> {
        self.hybrid.search_dense_only(query_vector, top_k).await
    }

    /// Sparse-only fallback (BM25).
    pub async fn query_sparse(&self, query_text: &str, top_k: usize) -> Result<Vec<RagHit>> {
        self.hybrid.search_sparse_only(query_text, top_k).await
    }
}

impl std::fmt::Debug for VectorStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("VectorStore")
            .field("hybrid", &self.hybrid)
            .finish_non_exhaustive()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hybrid::HitSource;
    use tempfile::TempDir;

    /// Build a tiny in-memory store: 1 file, 3 chunks with 4-dim vectors.
    async fn tiny_store() -> (VectorStore, TempDir) {
        let tmp = TempDir::new().unwrap();
        let sqlite_path = tmp.path().join("kb.sqlite");
        let sqlite = SqliteStore::open(&sqlite_path).await.unwrap();

        let file_id = sqlite
            .insert_file("公共/fixture.md", "公共", "abc", 1, 1)
            .await
            .unwrap();

        let corpus = [
            ("apple banana cherry", [1.0_f32, 0.0, 0.0, 0.0]),
            ("dog elephant fox", [0.0, 1.0, 0.0, 0.0]),
            ("grape honey iris", [0.0, 0.0, 1.0, 0.0]),
        ];
        let mut chunk_ids = [0_i64; 3];
        for (i, (text, vec)) in corpus.iter().enumerate() {
            chunk_ids[i] = sqlite
                .insert_chunk(file_id, i as i64, text, Some(vec))
                .await
                .unwrap();
        }

        let mut index = UsearchIndex::create_with_capacity(4, 16).unwrap();
        for (i, (_, vec)) in corpus.iter().enumerate() {
            index.add(chunk_ids[i] as u64, vec).unwrap();
        }

        (VectorStore::from_parts(sqlite, index), tmp)
    }

    #[tokio::test]
    async fn hybrid_query_surfaces_matching_chunk() {
        let (store, _tmp) = tiny_store().await;
        // BM25 exact-matches "banana", dense-exact-matches the first
        // vector: RRF should agree → chunk 0 first.
        let hits = store
            .query("banana", &[1.0, 0.0, 0.0, 0.0], 3)
            .await
            .unwrap();
        assert!(!hits.is_empty());
        assert!(hits[0].content.contains("banana"));
        assert_eq!(hits[0].path, "公共/fixture.md");
        // RRF tags it as "Both" when the same doc wins on both sides.
        assert_eq!(hits[0].source, HitSource::Both);
    }

    #[tokio::test]
    async fn query_top_k_zero_is_empty() {
        let (store, _tmp) = tiny_store().await;
        let hits = store
            .query("banana", &[1.0, 0.0, 0.0, 0.0], 0)
            .await
            .unwrap();
        assert!(hits.is_empty());
    }

    #[tokio::test]
    async fn query_dense_only_works() {
        let (store, _tmp) = tiny_store().await;
        let hits = store.query_dense(&[1.0, 0.0, 0.0, 0.0], 2).await.unwrap();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].source, HitSource::Dense);
    }

    #[tokio::test]
    async fn query_sparse_only_works() {
        let (store, _tmp) = tiny_store().await;
        let hits = store.query_sparse("elephant", 2).await.unwrap();
        assert!(!hits.is_empty());
        assert!(hits[0].content.contains("elephant"));
        assert_eq!(hits[0].source, HitSource::Sparse);
    }

    #[tokio::test]
    async fn query_empty_index_is_empty_when_text_also_empty() {
        let tmp = TempDir::new().unwrap();
        let sqlite = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        let index = UsearchIndex::create_with_capacity(4, 4).unwrap();
        let store = VectorStore::from_parts(sqlite, index);
        let hits = store.query("", &[1.0, 0.0, 0.0, 0.0], 5).await.unwrap();
        assert!(hits.is_empty());
    }
}
