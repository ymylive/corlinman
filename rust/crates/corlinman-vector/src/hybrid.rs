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
//! # Cross-encoder rerank
//!
//! A post-RRF rerank stage is pluggable via [`crate::rerank::Reranker`].
//! The searcher holds an `Arc<dyn Reranker>` (default
//! [`crate::rerank::NoopReranker`]) and — when `params.rerank_enabled` is
//! `true` — hands the fused hits to it before truncating to `top_k`.
//! Sprint 3 T6 shipped the trait + a noop default + a
//! [`crate::rerank::GrpcReranker`] stub; the real client lives in the
//! Python embedding service (see `corlinman_embedding.rerank_client`).
//!
//! # Not yet implemented
//!
//! - LRU unload of `.usearch` files on idle timeout.
//! - `Rerank` gRPC RPC in `proto/embedding.proto` (the stub in
//!   [`crate::rerank::GrpcReranker`] currently returns
//!   `CorlinmanError::Internal("unimplemented: ...")`).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use corlinman_core::metrics::VECTOR_QUERY_DURATION;
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

use crate::rerank::{NoopReranker, Reranker};
use crate::sqlite::SqliteStore;
use crate::usearch_index::UsearchIndex;

/// Tag-filter predicate pushed down to both recall paths (Sprint 3 T4).
///
/// Semantics (all conditions conjoined):
/// - `required`: chunk must carry *every* tag in the list.
/// - `any_of`: chunk must carry *at least one* tag (ignored when empty).
/// - `excluded`: chunk must carry *none* of the listed tags.
///
/// An all-empty `TagFilter` is equivalent to `None` — callers should not
/// build one in that case (the searcher still short-circuits correctly
/// if they do, it's just wasted work).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct TagFilter {
    pub required: Vec<String>,
    pub excluded: Vec<String>,
    pub any_of: Vec<String>,
}

impl TagFilter {
    /// `true` when every constraint list is empty.
    pub fn is_empty(&self) -> bool {
        self.required.is_empty() && self.excluded.is_empty() && self.any_of.is_empty()
    }
}

/// Reciprocal-rank-fusion tuning knobs.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
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
    /// Optional tag-filter predicate. `None` ⇒ no filter; see
    /// [`TagFilter`] for semantics. Pushed down into BM25 (SQL `IN` on
    /// the whitelisted ids) and post-filters HNSW (usearch has no
    /// predicate support, so we over-fetch then prune).
    pub tag_filter: Option<TagFilter>,
    /// Sprint 9 T1: restrict the search to one or more `chunks.namespace`
    /// partitions. `None` preserves legacy behaviour → only the
    /// `"general"` namespace is searched. `Some(vec![])` is treated the
    /// same as `None` to keep JSON callers from accidentally killing
    /// recall. Multi-valued vectors union the listed namespaces.
    pub namespaces: Option<Vec<String>>,
    /// Run the [`HybridSearcher`]'s [`Reranker`] after RRF fusion when
    /// `true`. Default: `false`. When `false` the fused list is simply
    /// truncated to `top_k` (the noop reranker behaviour), so callers
    /// who leave this alone see the legacy RRF-only ordering.
    pub rerank_enabled: bool,
}

impl HybridParams {
    /// Library defaults: `top_k=10`, `overfetch=3`, equal weights, `k=60`,
    /// no tag filter, namespace unset (→ `"general"`), rerank disabled.
    pub const fn new() -> Self {
        Self {
            top_k: 10,
            overfetch_multiplier: 3,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 60.0,
            tag_filter: None,
            namespaces: None,
            rerank_enabled: false,
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
    reranker: Arc<dyn Reranker>,
}

impl HybridSearcher {
    /// Construct a searcher with the provided default `params`. Callers
    /// can still override per-query via [`Self::search`]'s `override_params`.
    ///
    /// The reranker defaults to [`NoopReranker`]. Use
    /// [`Self::with_reranker`] on the returned value to swap it.
    pub fn new(
        sqlite: Arc<SqliteStore>,
        usearch: Arc<RwLock<UsearchIndex>>,
        params: HybridParams,
    ) -> Self {
        Self {
            sqlite,
            usearch,
            params,
            reranker: Arc::new(NoopReranker),
        }
    }

    /// Replace the reranker. Returns `self` for builder-style chaining:
    ///
    /// ```ignore
    /// let searcher = HybridSearcher::new(sqlite, usearch, params)
    ///     .with_reranker(Arc::new(GrpcReranker::new("http://...", "bge-reranker-v2-m3")));
    /// ```
    ///
    /// Only takes effect for queries that also pass
    /// `params.rerank_enabled = true` (per-query override or via the
    /// searcher default).
    #[must_use]
    pub fn with_reranker(mut self, reranker: Arc<dyn Reranker>) -> Self {
        self.reranker = reranker;
        self
    }

    /// Borrow the active reranker (primarily for tests + introspection).
    pub fn reranker(&self) -> &Arc<dyn Reranker> {
        &self.reranker
    }

    /// Default parameters used when a `search` call passes `None`.
    pub fn params(&self) -> HybridParams {
        self.params.clone()
    }

    /// Hybrid search: HNSW + BM25 + RRF fusion.
    ///
    /// `query_text` drives BM25. `query_vector` drives HNSW. Pass an
    /// empty `query_text` to run dense-only implicitly (BM25 returns no
    /// hits and RRF reduces to the HNSW ranking).
    ///
    /// When `override_params.tag_filter` (or the default `params.tag_filter`)
    /// is `Some`, both recall paths are restricted to the intersection
    /// of `chunks.id` with the filter predicate:
    /// - BM25: SQL-level `rowid IN (...)` pushdown
    ///   ([`SqliteStore::search_bm25_with_filter`]).
    /// - HNSW: we over-fetch `fetch` candidates and drop any whose
    ///   `chunk_id` is not on the whitelist; usearch has no predicate
    ///   support so this is the best we can do without paginating.
    ///
    /// Sprint 9 T1: `params.namespaces` further restricts both paths to
    /// chunks whose `namespace` is on the list. `None` (or empty-vec)
    /// defaults to `["general"]` so legacy callers — none of whom set
    /// the field — see the same single-namespace recall they used
    /// before S9. The namespace whitelist intersects with `tag_filter`
    /// when both are set.
    pub async fn search(
        &self,
        query_text: &str,
        query_vector: &[f32],
        override_params: Option<HybridParams>,
    ) -> Result<Vec<RagHit>> {
        let p = override_params.unwrap_or_else(|| self.params.clone());
        if p.top_k == 0 {
            return Ok(Vec::new());
        }
        let fetch = p.top_k.saturating_mul(p.overfetch_multiplier.max(1));

        // --- Tag filter: resolve once, reuse for both paths. ---------------
        let tag_ids: Option<Vec<i64>> = match &p.tag_filter {
            Some(tf) if !tf.is_empty() => Some(
                self.sqlite
                    .filter_chunk_ids_by_tags(tf)
                    .await
                    .context("tag filter pushdown")?,
            ),
            _ => None,
        };

        // --- Namespace filter (S9 T1). Default = ["general"] ---------------
        let ns_ids: Vec<i64> = {
            let effective: Vec<String> = match &p.namespaces {
                Some(v) if !v.is_empty() => v.clone(),
                _ => vec!["general".to_string()],
            };
            self.sqlite
                .filter_chunk_ids_by_namespace(&effective)
                .await
                .context("namespace filter pushdown")?
        };

        // Combine namespace + tag filter. Namespace is always active, so
        // `allowed_ids` is always `Some` from S9 onwards. Intersection
        // preserves the stricter of the two when a caller supplies both.
        let allowed_ids: Option<Vec<i64>> = match tag_ids {
            None => Some(ns_ids),
            Some(tags) => {
                let ns_set: std::collections::HashSet<i64> = ns_ids.into_iter().collect();
                Some(tags.into_iter().filter(|id| ns_set.contains(id)).collect())
            }
        };
        let allowed_set: Option<std::collections::HashSet<i64>> =
            allowed_ids.as_ref().map(|v| v.iter().copied().collect());

        // Active filter + empty whitelist ⇒ no chunks match, skip the work.
        if matches!(&allowed_set, Some(s) if s.is_empty()) {
            return Ok(Vec::new());
        }

        // --- Recall path 1: HNSW (dense) -----------------------------------
        // S7.T3: record `corlinman_vector_query_duration_seconds{stage=hnsw}`.
        let hnsw_start = Instant::now();
        let dense_hits: Vec<(i64, f32)> = {
            let idx = self.usearch.read().await;
            if idx.size() == 0 || query_vector.is_empty() {
                Vec::new()
            } else {
                // Over-fetch when tag-filter is active: HNSW can't predicate,
                // so we pull extra and keep the first `fetch` survivors.
                let hnsw_k = if allowed_set.is_some() {
                    fetch.saturating_mul(4).max(fetch)
                } else {
                    fetch
                };
                let raw = idx.search(query_vector, hnsw_k).context("hnsw search")?;
                let mut out: Vec<(i64, f32)> = raw
                    .into_iter()
                    .map(|(k, dist)| (k as i64, 1.0 - dist))
                    .collect();
                if let Some(set) = &allowed_set {
                    out.retain(|(id, _)| set.contains(id));
                    out.truncate(fetch);
                }
                out
            }
        };
        VECTOR_QUERY_DURATION
            .with_label_values(&["hnsw"])
            .observe(hnsw_start.elapsed().as_secs_f64());

        // --- Recall path 2: BM25 (sparse) ----------------------------------
        let bm25_start = Instant::now();
        let sparse_hits: Vec<(i64, f32)> = self
            .sqlite
            .search_bm25_with_filter(query_text, fetch, allowed_ids.as_deref())
            .await
            .context("bm25 search")?;
        VECTOR_QUERY_DURATION
            .with_label_values(&["bm25"])
            .observe(bm25_start.elapsed().as_secs_f64());

        // --- Fusion --------------------------------------------------------
        //
        // When rerank is disabled we can truncate before hydration (the old
        // path). When rerank is enabled we keep the full fused set so the
        // cross-encoder has real candidates to re-order; truncation to
        // `top_k` happens inside the reranker.
        let fuse_start = Instant::now();
        let fused = rrf_fuse(&dense_hits, &sparse_hits, &p);
        let candidates: Vec<(i64, f32, HitSource)> = if p.rerank_enabled {
            fused
        } else {
            fused.into_iter().take(p.top_k).collect()
        };
        VECTOR_QUERY_DURATION
            .with_label_values(&["fuse"])
            .observe(fuse_start.elapsed().as_secs_f64());

        if candidates.is_empty() {
            return Ok(Vec::new());
        }

        let ids: Vec<i64> = candidates.iter().map(|(id, _, _)| *id).collect();
        let hits = self.hydrate(&ids, candidates).await?;

        // --- Optional rerank ----------------------------------------------
        if p.rerank_enabled {
            let rerank_start = Instant::now();
            let out = self
                .reranker
                .rerank(query_text, hits, p.top_k)
                .await
                .map_err(|e| anyhow::anyhow!("reranker failed: {e}"));
            VECTOR_QUERY_DURATION
                .with_label_values(&["rerank"])
                .observe(rerank_start.elapsed().as_secs_f64());
            out
        } else {
            Ok(hits)
        }
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
            .field("reranker", &self.reranker)
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
            tag_filter: None,
            namespaces: None,
            rerank_enabled: false,
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
            tag_filter: None,
            namespaces: None,
            rerank_enabled: false,
        };
        let fused = rrf_fuse(&[(1, 0.0)], &[(1, 0.0)], &p);
        assert_eq!(fused.len(), 1);
        assert!(fused[0].1.is_finite());
    }

    // ---- tag filter integration ---------------------------------------

    use tempfile::TempDir;

    /// Build a tiny hybrid searcher: 3 chunks of 4-d vectors, first two
    /// tagged, the third untagged. Used by the tag-filter tests.
    async fn tagged_store() -> (HybridSearcher, TempDir) {
        let tmp = TempDir::new().unwrap();
        let sqlite = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        let file_id = sqlite
            .insert_file("notes/t.md", "notes", "h", 0, 0)
            .await
            .unwrap();

        let corpus = [
            ("apple banana cherry", [1.0_f32, 0.0, 0.0, 0.0]),
            ("banana dog elephant", [0.9, 0.1, 0.0, 0.0]),
            ("grape honey iris", [0.0, 0.0, 1.0, 0.0]),
        ];
        let mut ids = [0_i64; 3];
        for (i, (text, vec)) in corpus.iter().enumerate() {
            ids[i] = sqlite
                .insert_chunk(file_id, i as i64, text, Some(vec), "general")
                .await
                .unwrap();
        }
        // ids[0] → rust+backend; ids[1] → rust+frontend; ids[2] → untagged.
        sqlite.insert_tag(ids[0], "rust").await.unwrap();
        sqlite.insert_tag(ids[0], "backend").await.unwrap();
        sqlite.insert_tag(ids[1], "rust").await.unwrap();
        sqlite.insert_tag(ids[1], "frontend").await.unwrap();

        let mut index = UsearchIndex::create_with_capacity(4, 16).unwrap();
        for (i, (_, vec)) in corpus.iter().enumerate() {
            index.add(ids[i] as u64, vec).unwrap();
        }

        let hybrid = HybridSearcher::new(
            Arc::new(sqlite),
            Arc::new(RwLock::new(index)),
            HybridParams::new(),
        );
        (hybrid, tmp)
    }

    fn params_with_filter(top_k: usize, tf: TagFilter) -> HybridParams {
        HybridParams {
            top_k,
            overfetch_multiplier: 3,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 60.0,
            tag_filter: Some(tf),
            namespaces: None,
            rerank_enabled: false,
        }
    }

    #[tokio::test]
    async fn tag_filter_required_matches_only_those_tags() {
        let (searcher, _tmp) = tagged_store().await;
        let tf = TagFilter {
            required: vec!["rust".into()],
            ..Default::default()
        };
        // "banana" matches chunks 0 and 1; both carry "rust" so both survive.
        // chunk 2 ("grape honey iris") has no "rust" tag → excluded.
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(params_with_filter(10, tf)),
            )
            .await
            .unwrap();
        assert_eq!(hits.len(), 2);
        for h in &hits {
            assert!(!h.content.contains("grape"));
        }
    }

    #[tokio::test]
    async fn tag_filter_excluded_removes_matches() {
        let (searcher, _tmp) = tagged_store().await;
        let tf = TagFilter {
            excluded: vec!["frontend".into()],
            ..Default::default()
        };
        // chunk 1 is tagged frontend → excluded. chunks 0 and 2 pass.
        let hits = searcher
            .search(
                "banana grape",
                &[1.0, 0.0, 0.0, 0.0],
                Some(params_with_filter(10, tf)),
            )
            .await
            .unwrap();
        let contents: Vec<&str> = hits.iter().map(|h| h.content.as_str()).collect();
        assert!(contents.iter().any(|c| c.contains("apple")));
        // "banana dog elephant" is the frontend-tagged chunk — must be gone.
        assert!(!contents.iter().any(|c| c.contains("dog elephant")));
    }

    #[tokio::test]
    async fn tag_filter_any_of_ors() {
        let (searcher, _tmp) = tagged_store().await;
        let tf = TagFilter {
            any_of: vec!["backend".into(), "frontend".into()],
            ..Default::default()
        };
        // chunks 0 (backend) and 1 (frontend) qualify; chunk 2 does not.
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(params_with_filter(10, tf)),
            )
            .await
            .unwrap();
        assert_eq!(hits.len(), 2);
    }

    #[tokio::test]
    async fn tag_filter_empty_equivalent_to_no_filter() {
        let (searcher, _tmp) = tagged_store().await;
        let with_empty = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(params_with_filter(10, TagFilter::default())),
            )
            .await
            .unwrap();
        let without = searcher
            .search("banana", &[1.0, 0.0, 0.0, 0.0], None)
            .await
            .unwrap();
        assert_eq!(with_empty.len(), without.len());
    }

    #[tokio::test]
    async fn tag_filter_combined_required_and_excluded() {
        let (searcher, _tmp) = tagged_store().await;
        let tf = TagFilter {
            required: vec!["rust".into()],
            excluded: vec!["frontend".into()],
            ..Default::default()
        };
        // Only chunk 0 satisfies rust ∧ ¬frontend.
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(params_with_filter(10, tf)),
            )
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert!(hits[0].content.contains("apple"));
    }

    // ---- namespace filter (Sprint 9 T1) -------------------------------

    /// Seed a searcher with 4 chunks split across two namespaces:
    /// - ids[0..2] → `general` ("apple banana cherry", "banana dog")
    /// - ids[2..4] → `diary:a`  ("banana rain", "banana snow")
    async fn namespaced_store() -> (HybridSearcher, TempDir) {
        let tmp = TempDir::new().unwrap();
        let sqlite = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        let file_id = sqlite.insert_file("ns.md", "ns", "h", 0, 0).await.unwrap();

        let rows: &[(&str, [f32; 4], &str)] = &[
            ("apple banana cherry", [1.0, 0.0, 0.0, 0.0], "general"),
            ("banana dog", [0.9, 0.1, 0.0, 0.0], "general"),
            ("banana rain", [0.8, 0.0, 0.2, 0.0], "diary:a"),
            ("banana snow", [0.0, 1.0, 0.0, 0.0], "diary:a"),
        ];
        let mut ids = [0_i64; 4];
        let mut index = UsearchIndex::create_with_capacity(4, 16).unwrap();
        for (i, (text, v, ns)) in rows.iter().enumerate() {
            ids[i] = sqlite
                .insert_chunk(file_id, i as i64, text, Some(v), ns)
                .await
                .unwrap();
            index.add(ids[i] as u64, v).unwrap();
        }
        let hybrid = HybridSearcher::new(
            Arc::new(sqlite),
            Arc::new(RwLock::new(index)),
            HybridParams::new(),
        );
        (hybrid, tmp)
    }

    fn ns_params(namespaces: Option<Vec<String>>) -> HybridParams {
        HybridParams {
            top_k: 10,
            overfetch_multiplier: 3,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 60.0,
            tag_filter: None,
            namespaces,
            rerank_enabled: false,
        }
    }

    #[tokio::test]
    async fn namespace_filter_restricts_to_named_namespace() {
        let (searcher, _tmp) = namespaced_store().await;
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(ns_params(Some(vec!["diary:a".into()]))),
            )
            .await
            .unwrap();
        // Only the two diary:a rows should survive.
        assert_eq!(
            hits.len(),
            2,
            "got: {:?}",
            hits.iter().map(|h| &h.content).collect::<Vec<_>>()
        );
        for h in &hits {
            assert!(
                h.content.contains("rain") || h.content.contains("snow"),
                "unexpected leakage: {}",
                h.content
            );
        }
    }

    #[tokio::test]
    async fn namespace_none_defaults_to_general_only() {
        // Legacy callers who don't set `namespaces` must continue to see
        // the pre-S9 behaviour: only the `general` namespace is searched.
        let (searcher, _tmp) = namespaced_store().await;
        let hits = searcher
            .search("banana", &[1.0, 0.0, 0.0, 0.0], Some(ns_params(None)))
            .await
            .unwrap();
        // 2 general rows — 0 diary:a rows.
        assert_eq!(hits.len(), 2);
        for h in &hits {
            assert!(
                h.content.contains("apple") || h.content.contains("dog"),
                "non-general leaked: {}",
                h.content
            );
        }
    }

    #[tokio::test]
    async fn namespace_empty_vec_treated_as_none() {
        let (searcher, _tmp) = namespaced_store().await;
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(ns_params(Some(vec![]))),
            )
            .await
            .unwrap();
        assert_eq!(hits.len(), 2); // same as None → general only.
    }

    #[tokio::test]
    async fn namespace_multi_value_union() {
        let (searcher, _tmp) = namespaced_store().await;
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(ns_params(Some(vec!["general".into(), "diary:a".into()]))),
            )
            .await
            .unwrap();
        // All 4 rows match "banana".
        assert_eq!(hits.len(), 4);
    }

    #[tokio::test]
    async fn list_namespaces_counts_rows_per_namespace() {
        let (searcher, _tmp) = namespaced_store().await;
        let nss = searcher.sqlite.list_namespaces().await.unwrap();
        assert_eq!(
            nss,
            vec![("diary:a".to_string(), 2u64), ("general".to_string(), 2u64),]
        );
    }

    // ---- reranker integration (Sprint 3 T6) ----------------------------

    use crate::rerank::Reranker;
    use async_trait::async_trait;

    /// Reverses the order RRF produced, so we can observe whether the
    /// searcher actually consulted the injected reranker.
    #[derive(Debug, Default)]
    struct ReversingReranker;

    #[async_trait]
    impl Reranker for ReversingReranker {
        async fn rerank(
            &self,
            _query: &str,
            mut hits: Vec<RagHit>,
            top_k: usize,
        ) -> Result<Vec<RagHit>, corlinman_core::error::CorlinmanError> {
            hits.reverse();
            hits.truncate(top_k);
            Ok(hits)
        }
    }

    fn rerank_params(top_k: usize, enabled: bool) -> HybridParams {
        HybridParams {
            top_k,
            overfetch_multiplier: 3,
            bm25_weight: 1.0,
            hnsw_weight: 1.0,
            rrf_k: 60.0,
            tag_filter: None,
            namespaces: None,
            rerank_enabled: enabled,
        }
    }

    #[tokio::test]
    async fn rerank_disabled_preserves_rrf_order() {
        let (searcher, _tmp) = tagged_store().await;
        // Even with a reversing reranker installed, `rerank_enabled=false`
        // must leave the RRF ordering intact.
        let searcher = searcher.with_reranker(Arc::new(ReversingReranker));
        let hits = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(rerank_params(10, false)),
            )
            .await
            .unwrap();
        assert!(!hits.is_empty());
        // "apple banana cherry" is the closest dense match (vector [1,0,0,0])
        // and also wins BM25 on "banana" → it should lead the RRF output.
        assert!(
            hits[0].content.contains("apple"),
            "expected RRF top to be the apple chunk, got {:?}",
            hits.iter().map(|h| &h.content).collect::<Vec<_>>()
        );
    }

    #[tokio::test]
    async fn rerank_enabled_uses_injected_reranker() {
        let (searcher, _tmp) = tagged_store().await;
        let baseline = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(rerank_params(10, false)),
            )
            .await
            .unwrap();

        let searcher = searcher.with_reranker(Arc::new(ReversingReranker));
        let reranked = searcher
            .search(
                "banana",
                &[1.0, 0.0, 0.0, 0.0],
                Some(rerank_params(10, true)),
            )
            .await
            .unwrap();

        assert_eq!(baseline.len(), reranked.len());
        assert!(baseline.len() >= 2, "need ≥2 hits to test reversal");
        // Reranker reverses: the former last should be first, former first last.
        assert_eq!(
            reranked.first().unwrap().chunk_id,
            baseline.last().unwrap().chunk_id
        );
        assert_eq!(
            reranked.last().unwrap().chunk_id,
            baseline.first().unwrap().chunk_id
        );
    }

    /// S7.T3: each `search()` call records into
    /// `corlinman_vector_query_duration_seconds` for the three core
    /// stages (`hnsw`, `bm25`, `fuse`). Counters are process-global so
    /// other concurrent tests may also observe — we assert the deltas
    /// are non-zero rather than exact.
    #[tokio::test]
    async fn search_records_stage_metrics() {
        let (searcher, _tmp) = tagged_store().await;

        let hnsw_before = VECTOR_QUERY_DURATION
            .with_label_values(&["hnsw"])
            .get_sample_count();
        let bm25_before = VECTOR_QUERY_DURATION
            .with_label_values(&["bm25"])
            .get_sample_count();
        let fuse_before = VECTOR_QUERY_DURATION
            .with_label_values(&["fuse"])
            .get_sample_count();

        let _ = searcher
            .search("banana", &[1.0, 0.0, 0.0, 0.0], None)
            .await
            .unwrap();

        let hnsw_after = VECTOR_QUERY_DURATION
            .with_label_values(&["hnsw"])
            .get_sample_count();
        let bm25_after = VECTOR_QUERY_DURATION
            .with_label_values(&["bm25"])
            .get_sample_count();
        let fuse_after = VECTOR_QUERY_DURATION
            .with_label_values(&["fuse"])
            .get_sample_count();

        assert!(
            hnsw_after > hnsw_before,
            "hnsw stage must record at least one observation"
        );
        assert!(
            bm25_after > bm25_before,
            "bm25 stage must record at least one observation"
        );
        assert!(
            fuse_after > fuse_before,
            "fuse stage must record at least one observation"
        );
    }

    #[tokio::test]
    async fn rerank_enabled_truncates_to_top_k() {
        let (searcher, _tmp) = tagged_store().await;
        let searcher = searcher.with_reranker(Arc::new(ReversingReranker));
        // Corpus has 3 chunks; ask for top_k=2 with rerank on.
        let hits = searcher
            .search(
                "banana grape",
                &[1.0, 0.0, 0.0, 0.0],
                Some(rerank_params(2, true)),
            )
            .await
            .unwrap();
        assert!(hits.len() <= 2);
    }
}
