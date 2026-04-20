//! Hybrid HNSW + BM25 retrieval with reciprocal-rank-fusion.
//!
//! # Strategy
//!
//! For each query we run two recall paths in parallel:
//!
//! 1. **Dense (HNSW)** via [`crate::usearch_index::UsearchIndex::search`],
//!    metric = cosine.
//! 2. **Sparse (BM25)** via [`crate::sqlite::SqliteStore::search_bm25`],
//!    using the FTS5 `bm25()` ranker.
//!
//! Each path is queried for `top_k * overfetch_multiplier` candidates so
//! RRF has enough signal to re-rank. We then merge with
//!
//! ```text
//!   rrf_score(doc) = Σ_r  weight_r / (rrf_k + rank_r(doc))
//! ```
//!
//! over rankers `r ∈ {dense, sparse}`. Documents missing from one path
//! contribute `0` from that path — there is no implicit last-rank
//! penalty. The final list is sorted by fused score (descending) and
//! truncated to `top_k`.
//!
//! # Not yet implemented
//!
//! - Cross-encoder rerank on top of RRF output (planned for M6).
//! - LRU unload of `.usearch` files on idle timeout.
//! - Tag / metadata filter pushdown (the baseline SQLite schema no
//!   longer carries tag tables in M4).

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

use crate::sqlite::SqliteStore;
use crate::usearch_index::UsearchIndex;

/// Reciprocal-rank-fusion tuning knobs.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct HybridParams {
    /// Final number of fused hits to return.
    pub top_k: usize,
    /// Each recall path is asked for `top_k * overfetch_multiplier`
    /// candidates. `1` disables overfetch.
    pub overfetch_multiplier: usize,
    /// Weight applied to the BM25 (sparse) ranker in the RRF sum.
    pub bm25_weight: f32,
    /// Weight applied to the HNSW (dense) ranker in the RRF sum.
    pub hnsw_weight: f32,
    /// RRF dampening constant `k` (standard literature default = 60).
    pub rrf_k: f32,
}

impl HybridParams {
    /// Library defaults: `top_k=10`, `overfetch=3`, equal weights, `k=60`.
    pub const fn new() -> Self {
        Self {
            top_k: 10,
            overfetch_multiplier: 3,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 60.0,
        }
    }
}

impl Default for HybridParams {
    fn default() -> Self {
        Self::new()
    }
}

/// Which recall path(s) surfaced a given hit.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub enum HitSource {
    /// Dense (HNSW) only.
    Dense,
    /// Sparse (BM25) only.
    Sparse,
    /// Both paths returned the chunk — typically the most trustworthy.
    Both,
}

/// One hit emitted by the hybrid searcher.
///
/// `score` is the fused RRF value (larger = better). Pure-path hits
/// returned by [`HybridSearcher::search_dense_only`] /
/// [`HybridSearcher::search_sparse_only`] carry the raw path score
/// instead (cosine-similarity or negated-bm25).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RagHit {
    pub chunk_id: i64,
    pub file_id: i64,
    pub content: String,
    pub score: f32,
    pub source: HitSource,
    pub path: String,
}

/// Owns the two storage backends + default fusion parameters.
///
/// The usearch index sits behind an `RwLock` so index writes (add /
/// save) can proceed without blocking concurrent reads once we wire the
/// indexer in later milestones.
#[derive(Clone)]
pub struct HybridSearcher {
    sqlite: Arc<SqliteStore>,
    usearch: Arc<RwLock<UsearchIndex>>,
    params: HybridParams,
}

impl HybridSearcher {
    /// Construct a searcher with the provided default `params`. Callers
    /// can still override per-query via [`Self::search`]'s `override_params`.
    pub fn new(
        sqlite: Arc<SqliteStore>,
        usearch: Arc<RwLock<UsearchIndex>>,
        params: HybridParams,
    ) -> Self {
        Self {
            sqlite,
            usearch,
            params,
        }
    }

    /// Default parameters used when a `search` call passes `None`.
    pub fn params(&self) -> HybridParams {
        self.params
    }

    /// Hybrid search: HNSW + BM25 + RRF fusion.
    ///
    /// `query_text` drives BM25. `query_vector` drives HNSW. Pass an
    /// empty `query_text` to run dense-only implicitly (BM25 returns no
    /// hits and RRF reduces to the HNSW ranking).
    pub async fn search(
        &self,
        query_text: &str,
        query_vector: &[f32],
        override_params: Option<HybridParams>,
    ) -> Result<Vec<RagHit>> {
        let p = override_params.unwrap_or(self.params);
        if p.top_k == 0 {
            return Ok(Vec::new());
        }
        let fetch = p.top_k.saturating_mul(p.overfetch_multiplier.max(1));

        // --- Recall path 1: HNSW (dense) -----------------------------------
        let dense_hits: Vec<(i64, f32)> = {
            let idx = self.usearch.read().await;
            if idx.size() == 0 || query_vector.is_empty() {
                Vec::new()
            } else {
                idx.search(query_vector, fetch)
                    .context("hnsw search")?
                    .into_iter()
                    .map(|(k, dist)| (k as i64, 1.0 - dist))
                    .collect()
            }
        };

        // --- Recall path 2: BM25 (sparse) ----------------------------------
        let sparse_hits: Vec<(i64, f32)> = self
            .sqlite
            .search_bm25(query_text, fetch)
            .await
            .context("bm25 search")?;

        // --- Fusion --------------------------------------------------------
        let fused = rrf_fuse(&dense_hits, &sparse_hits, &p);
        let truncated: Vec<(i64, f32, HitSource)> = fused.into_iter().take(p.top_k).collect();
        if truncated.is_empty() {
            return Ok(Vec::new());
        }

        let ids: Vec<i64> = truncated.iter().map(|(id, _, _)| *id).collect();
        self.hydrate(&ids, truncated).await
    }

    /// HNSW-only fallback. Bypasses RRF; score is cosine similarity.
    pub async fn search_dense_only(
        &self,
        query_vector: &[f32],
        top_k: usize,
    ) -> Result<Vec<RagHit>> {
        if top_k == 0 || query_vector.is_empty() {
            return Ok(Vec::new());
        }
        let idx = self.usearch.read().await;
        if idx.size() == 0 {
            return Ok(Vec::new());
        }
        let raw = idx.search(query_vector, top_k).context("hnsw search")?;
        drop(idx);

        let scored: Vec<(i64, f32, HitSource)> = raw
            .into_iter()
            .map(|(k, dist)| (k as i64, 1.0 - dist, HitSource::Dense))
            .collect();
        let ids: Vec<i64> = scored.iter().map(|(id, _, _)| *id).collect();
        self.hydrate(&ids, scored).await
    }

    /// BM25-only fallback. Bypasses RRF; score is the negated-bm25
    /// value (positive, larger = better).
    pub async fn search_sparse_only(&self, query_text: &str, top_k: usize) -> Result<Vec<RagHit>> {
        if top_k == 0 || query_text.trim().is_empty() {
            return Ok(Vec::new());
        }
        let raw = self
            .sqlite
            .search_bm25(query_text, top_k)
            .await
            .context("bm25 search")?;
        let scored: Vec<(i64, f32, HitSource)> = raw
            .into_iter()
            .map(|(id, score)| (id, score, HitSource::Sparse))
            .collect();
        let ids: Vec<i64> = scored.iter().map(|(id, _, _)| *id).collect();
        self.hydrate(&ids, scored).await
    }

    /// Turn (chunk_id, score, source) triples into full [`RagHit`]s by
    /// joining content + file path, preserving the input order.
    async fn hydrate(
        &self,
        chunk_ids: &[i64],
        scored: Vec<(i64, f32, HitSource)>,
    ) -> Result<Vec<RagHit>> {
        let chunks = self
            .sqlite
            .query_chunks_by_ids(chunk_ids)
            .await
            .context("chunk hydration")?;
        if chunks.is_empty() {
            return Ok(Vec::new());
        }

        // Preload the files table — tiny relative to chunks, and avoids
        // an N+1 pattern across distinct file_ids.
        let files = self.sqlite.list_files().await.context("list_files")?;
        let path_by_file: HashMap<i64, String> =
            files.into_iter().map(|f| (f.id, f.path)).collect();

        // Index chunks by id so the output preserves `scored`'s order.
        let chunk_by_id: HashMap<i64, crate::sqlite::ChunkRow> =
            chunks.into_iter().map(|c| (c.id, c)).collect();

        let mut out = Vec::with_capacity(scored.len());
        for (id, score, source) in scored {
            let Some(c) = chunk_by_id.get(&id) else {
                continue; // ghost row: index refers to a missing chunk.
            };
            out.push(RagHit {
                chunk_id: c.id,
                file_id: c.file_id,
                content: c.content.clone(),
                score,
                source,
                path: path_by_file.get(&c.file_id).cloned().unwrap_or_default(),
            });
        }
        Ok(out)
    }
}

impl std::fmt::Debug for HybridSearcher {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HybridSearcher")
            .field("params", &self.params)
            .finish_non_exhaustive()
    }
}

/// Fuse two ranked lists with weighted reciprocal-rank-fusion.
///
/// `dense` and `sparse` are ordered best-first; their per-item float
/// scores are ignored — RRF only needs the rank. Returns
/// `(chunk_id, rrf_score, source)` sorted by descending RRF score.
fn rrf_fuse(
    dense: &[(i64, f32)],
    sparse: &[(i64, f32)],
    p: &HybridParams,
) -> Vec<(i64, f32, HitSource)> {
    let mut scores: HashMap<i64, (f32, bool, bool)> = HashMap::new();
    let k = p.rrf_k.max(1.0); // clamp to avoid div-by-zero if caller passes 0.

    for (rank, (id, _)) in dense.iter().enumerate() {
        let contrib = p.hnsw_weight / (k + (rank as f32 + 1.0));
        let entry = scores.entry(*id).or_insert((0.0, false, false));
        entry.0 += contrib;
        entry.1 = true;
    }
    for (rank, (id, _)) in sparse.iter().enumerate() {
        let contrib = p.bm25_weight / (k + (rank as f32 + 1.0));
        let entry = scores.entry(*id).or_insert((0.0, false, false));
        entry.0 += contrib;
        entry.2 = true;
    }

    let mut out: Vec<(i64, f32, HitSource)> = scores
        .into_iter()
        .map(|(id, (score, in_dense, in_sparse))| {
            let source = match (in_dense, in_sparse) {
                (true, true) => HitSource::Both,
                (true, false) => HitSource::Dense,
                (false, true) => HitSource::Sparse,
                (false, false) => unreachable!("score entry must come from at least one ranker"),
            };
            (id, score, source)
        })
        .collect();
    out.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0)) // stable tiebreak by id
    });
    out
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rrf_ranks_doc_in_both_paths_highest() {
        let dense = vec![(10, 0.99), (20, 0.80), (30, 0.50)];
        let sparse = vec![(30, 5.0), (20, 3.0), (40, 1.0)];
        let p = HybridParams::new();
        let fused = rrf_fuse(&dense, &sparse, &p);

        // Docs 20 and 30 appear in both; they should rank above 10 / 40.
        let top_ids: Vec<i64> = fused.iter().take(2).map(|(id, _, _)| *id).collect();
        assert!(
            top_ids.contains(&20) && top_ids.contains(&30),
            "top-2 should be the intersection, got {top_ids:?}"
        );
        // The source tag must reflect the intersection.
        for (id, _, source) in &fused {
            match *id {
                20 | 30 => assert_eq!(*source, HitSource::Both),
                10 => assert_eq!(*source, HitSource::Dense),
                40 => assert_eq!(*source, HitSource::Sparse),
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn rrf_weights_bias_path() {
        let dense = vec![(1, 0.9), (2, 0.8)];
        let sparse = vec![(2, 5.0), (1, 3.0)];

        // Bias heavily toward sparse: doc 2 (rank 1 in sparse) should win.
        let p = HybridParams {
            top_k: 2,
            overfetch_multiplier: 1,
            bm25_weight: 10.0,
            hnsw_weight: 0.1,
            rrf_k: 60.0,
        };
        let fused = rrf_fuse(&dense, &sparse, &p);
        assert_eq!(fused[0].0, 2);
    }

    #[test]
    fn rrf_handles_empty_inputs() {
        let p = HybridParams::new();
        assert!(rrf_fuse(&[], &[], &p).is_empty());
        let dense = vec![(1, 0.5)];
        let only_dense = rrf_fuse(&dense, &[], &p);
        assert_eq!(only_dense.len(), 1);
        assert_eq!(only_dense[0].2, HitSource::Dense);
    }

    #[test]
    fn rrf_k_clamped_at_one() {
        // rrf_k=0 must not panic with div-by-zero.
        let p = HybridParams {
            top_k: 1,
            overfetch_multiplier: 1,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 0.0,
        };
        let fused = rrf_fuse(&[(1, 0.0)], &[(1, 0.0)], &p);
        assert_eq!(fused.len(), 1);
        assert!(fused[0].1.is_finite());
    }
}
