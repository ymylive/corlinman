//! Thin wrapper around `usearch` 2.x covering the standard `.usearch` file format.
//!
//! usearch-rs 2.x reads/writes the standard usearch binary format directly
//! (see `docs/algorithms/vexus-vs-usearch.md` for design notes).
//!
//! # M4 滩头 scope
//!
//! - `open` / `create` / `save` / `add` / `search` / `size` / `dim`.
//! - Dimension is checked on every `add` / `search` — mismatched vectors fail
//!   loudly via [`anyhow::Error`] so the caller can't silently corrupt the
//!   index.
//! - Metric is fixed to **cosine**: results ranked by cosine similarity
//!   (descending) after transforming `1 - distance`.
//!
//! # M4 正式 TODO
//!
//! - LRU unload ([`IndexCache`]) honouring `KNOWLEDGEBASE_INDEX_IDLE_TTL_MS`.
//! - Save debouncing (`indexSaveDelay` / `tagIndexSaveDelay`).
//! - Duplicate-key upsert (`.remove()` then `.add()`).

use std::path::Path;

use anyhow::{anyhow, Result};
use usearch::{ffi::IndexOptions, Index, MetricKind, ScalarKind};

/// Default HNSW capacity used when creating a fresh index.
pub const DEFAULT_CAPACITY: usize = 50_000;

/// HNSW index over f32 cosine-distance vectors.
///
/// Wraps `usearch::Index` with the minimum surface area the M4 滩头 query
/// pipeline needs.
pub struct UsearchIndex {
    index: Index,
    dim: usize,
}

impl UsearchIndex {
    /// Create a new empty in-memory index with `dim` dimensions and
    /// [`DEFAULT_CAPACITY`] reserved slots.
    pub fn create(dim: usize) -> Result<Self> {
        Self::create_with_capacity(dim, DEFAULT_CAPACITY)
    }

    /// Same as [`create`] but lets the caller pick the initial capacity
    /// (used by tests that don't want to allocate 50k slots).
    pub fn create_with_capacity(dim: usize, capacity: usize) -> Result<Self> {
        let opts = IndexOptions {
            dimensions: dim,
            metric: MetricKind::Cos,
            quantization: ScalarKind::F32,
            // usearch picks sensible defaults (M=16, ef_construction=128,
            // ef_search=64) when these are 0 — keep that.
            connectivity: 0,
            expansion_add: 0,
            expansion_search: 0,
            multi: false,
        };
        let index = Index::new(&opts).map_err(|e| anyhow!("usearch Index::new failed: {e}"))?;
        index
            .reserve(capacity)
            .map_err(|e| anyhow!("usearch reserve({capacity}) failed: {e}"))?;
        Ok(Self { index, dim })
    }

    /// Open (load) an existing `.usearch` file.
    ///
    /// usearch writes the dimensionality into the file header; we read it
    /// back via [`Index::dimensions`] after `load`. A caller-supplied
    /// `expected_dim` can be enforced via [`open_checked`].
    pub fn open(path: &Path) -> Result<Self> {
        // `Index::load` requires an already-constructed Index. usearch
        // happily adopts the file's header metadata over whatever we put in
        // `IndexOptions`, but we need to construct *something* first.
        let opts = IndexOptions {
            dimensions: 1,
            metric: MetricKind::Cos,
            quantization: ScalarKind::F32,
            connectivity: 0,
            expansion_add: 0,
            expansion_search: 0,
            multi: false,
        };
        let index =
            Index::new(&opts).map_err(|e| anyhow!("usearch Index::new for load failed: {e}"))?;
        let path_str = path
            .to_str()
            .ok_or_else(|| anyhow!("non-UTF8 path: {}", path.display()))?;
        index
            .load(path_str)
            .map_err(|e| anyhow!("usearch load({}) failed: {e}", path.display()))?;
        let dim = index.dimensions();
        if dim == 0 {
            return Err(anyhow!("loaded index reports dim=0 (corrupt file?)"));
        }
        Ok(Self { index, dim })
    }

    /// [`open`] with a dimension assertion — returns a clear error if the
    /// file was built for a different embedding model.
    pub fn open_checked(path: &Path, expected_dim: usize) -> Result<Self> {
        let this = Self::open(path)?;
        if this.dim != expected_dim {
            return Err(anyhow!(
                "usearch dim mismatch: file={} expected={}",
                this.dim,
                expected_dim
            ));
        }
        Ok(this)
    }

    /// Save the index to disk. Parent directory must exist.
    pub fn save(&self, path: &Path) -> Result<()> {
        let path_str = path
            .to_str()
            .ok_or_else(|| anyhow!("non-UTF8 path: {}", path.display()))?;
        self.index
            .save(path_str)
            .map_err(|e| anyhow!("usearch save({}) failed: {e}", path.display()))?;
        Ok(())
    }

    /// Insert a (key, vector) pair.
    ///
    /// Returns an error if `vector.len() != self.dim`. Does **not** handle
    /// duplicate keys — that's a M4 正式 TODO (pattern: try-add → on
    /// Duplicate, remove + re-add).
    pub fn add(&mut self, key: u64, vector: &[f32]) -> Result<()> {
        if vector.len() != self.dim {
            return Err(anyhow!(
                "dim mismatch on add: got {} want {}",
                vector.len(),
                self.dim
            ));
        }
        // Auto-grow: usearch requires `reserve()` ahead of `size+1` rows, so
        // nudge capacity up when we're about to hit the limit.
        let cap = self.index.capacity();
        let size = self.index.size();
        if size + 1 > cap {
            let new_cap = (cap * 2).max(size + 16);
            self.index
                .reserve(new_cap)
                .map_err(|e| anyhow!("usearch grow reserve({new_cap}) failed: {e}"))?;
        }
        self.index
            .add(key, vector)
            .map_err(|e| anyhow!("usearch add(key={key}) failed: {e}"))?;
        Ok(())
    }

    /// Query the top-`k` nearest keys for `query`.
    ///
    /// Returns `(key, distance)` pairs; with cosine metric the distance is
    /// `1 - cosine_similarity`, so **smaller is more similar**. Callers that
    /// want a similarity score (larger is better) should transform via
    /// `1.0 - distance`.
    pub fn search(&self, query: &[f32], k: usize) -> Result<Vec<(u64, f32)>> {
        if query.len() != self.dim {
            return Err(anyhow!(
                "dim mismatch on search: got {} want {}",
                query.len(),
                self.dim
            ));
        }
        if k == 0 || self.index.size() == 0 {
            return Ok(Vec::new());
        }
        let matches = self
            .index
            .search(query, k)
            .map_err(|e| anyhow!("usearch search(k={k}) failed: {e}"))?;
        let out = matches
            .keys
            .into_iter()
            .zip(matches.distances)
            .collect::<Vec<_>>();
        Ok(out)
    }

    /// Number of vectors currently indexed.
    pub fn size(&self) -> usize {
        self.index.size()
    }

    /// Vector dimensionality.
    pub fn dim(&self) -> usize {
        self.dim
    }
}

impl std::fmt::Debug for UsearchIndex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UsearchIndex")
            .field("dim", &self.dim)
            .field("size", &self.index.size())
            .field("capacity", &self.index.capacity())
            .finish()
    }
}

/// Placeholder LRU cache for `.usearch` files loaded on demand.
///
/// Full M4 正式 implementation:
/// - `ttl = KNOWLEDGEBASE_INDEX_IDLE_TTL_MS` (2h default)
/// - background task sweeps every `KNOWLEDGEBASE_INDEX_IDLE_SWEEP_MS` (10min)
/// - on evict: save to disk, drop from memory
///
/// The M4 滩头 keeps a single un-evicting map so the query pipeline can be
/// wired end-to-end today.
#[derive(Debug, Default)]
pub struct IndexCache {
    // TODO(M4 正式): back with `DashMap<String, Arc<RwLock<UsearchIndex>>>`
    // + `DashMap<String, Instant>` for last-used timestamps.
    _marker: (),
}

impl IndexCache {
    pub fn new() -> Self {
        Self::default()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn create_add_search() {
        let mut idx = UsearchIndex::create_with_capacity(4, 16).unwrap();
        assert_eq!(idx.dim(), 4);
        assert_eq!(idx.size(), 0);

        idx.add(1, &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.add(2, &[0.0, 1.0, 0.0, 0.0]).unwrap();
        idx.add(3, &[0.9, 0.1, 0.0, 0.0]).unwrap();
        assert_eq!(idx.size(), 3);

        // Query ≈ [1, 0, 0, 0] → keys 1 and 3 should rank above 2.
        let hits = idx.search(&[1.0, 0.0, 0.0, 0.0], 3).unwrap();
        assert_eq!(hits.len(), 3);
        // First hit is key 1 (exact match).
        assert_eq!(hits[0].0, 1);
        // Distance for exact match ≈ 0.
        assert!(hits[0].1 < 1e-3, "dist={}", hits[0].1);
        // Key 3 (same direction, lower magnitude normalized) closer than 2.
        let pos_3 = hits.iter().position(|(k, _)| *k == 3).unwrap();
        let pos_2 = hits.iter().position(|(k, _)| *k == 2).unwrap();
        assert!(pos_3 < pos_2, "key 3 should rank above key 2");
    }

    #[test]
    fn save_and_reload() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("roundtrip.usearch");

        {
            let mut idx = UsearchIndex::create_with_capacity(3, 8).unwrap();
            idx.add(42, &[0.1, 0.2, 0.3]).unwrap();
            idx.add(99, &[0.9, 0.1, 0.0]).unwrap();
            idx.save(&path).unwrap();
        }

        let loaded = UsearchIndex::open(&path).unwrap();
        assert_eq!(loaded.dim(), 3);
        assert_eq!(loaded.size(), 2);

        let hits = loaded.search(&[0.1, 0.2, 0.3], 2).unwrap();
        assert_eq!(hits[0].0, 42);
    }

    #[test]
    fn dim_mismatch_is_error() {
        let mut idx = UsearchIndex::create_with_capacity(4, 8).unwrap();
        let err = idx.add(1, &[1.0, 2.0]).unwrap_err().to_string();
        assert!(err.contains("dim mismatch"), "{err}");
        let err = idx.search(&[1.0, 2.0], 1).unwrap_err().to_string();
        assert!(err.contains("dim mismatch"), "{err}");
    }

    #[test]
    fn search_on_empty_index_returns_empty() {
        let idx = UsearchIndex::create_with_capacity(4, 8).unwrap();
        let hits = idx.search(&[0.0, 0.0, 0.0, 1.0], 5).unwrap();
        assert!(hits.is_empty());
    }

    #[test]
    fn open_checked_rejects_wrong_dim() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("dim.usearch");
        {
            let idx = UsearchIndex::create_with_capacity(4, 8).unwrap();
            idx.save(&path).unwrap();
        }
        let err = UsearchIndex::open_checked(&path, 3)
            .unwrap_err()
            .to_string();
        assert!(err.contains("dim mismatch"), "{err}");

        let ok = UsearchIndex::open_checked(&path, 4).unwrap();
        assert_eq!(ok.dim(), 4);
    }

    #[test]
    fn auto_grow_on_add_past_capacity() {
        let mut idx = UsearchIndex::create_with_capacity(2, 2).unwrap();
        // Add more than initial capacity to trigger the reserve-grow path.
        for i in 0..8_u64 {
            let v = [i as f32, (i as f32) * 0.5];
            idx.add(i + 1, &v).unwrap();
        }
        assert_eq!(idx.size(), 8);
    }
}
