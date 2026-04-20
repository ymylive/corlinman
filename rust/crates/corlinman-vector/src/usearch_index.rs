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
//! # M4 正式
//!
//! - LRU unload ([`IndexCache`]) honouring `KNOWLEDGEBASE_INDEX_IDLE_TTL_MS`
//!   via a background sweeper task.
//! - Save debouncing ([`DebouncedSaver`]) to batch many `mark_dirty` into a
//!   single `save()` within a configurable window.
//! - Duplicate-key upsert ([`UsearchIndex::upsert`]) — `remove` then `add`.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use dashmap::DashMap;
use tokio::sync::{mpsc, RwLock};
use tokio_util::sync::CancellationToken;
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

    /// Idempotent insert: remove any existing entry for `key`, then add.
    ///
    /// usearch's `add` errors on a duplicate key, so callers that want
    /// "last write wins" semantics (re-indexing on chunk edit) go through
    /// this helper. `remove` is a no-op when the key isn't present; we
    /// deliberately swallow its result to keep the operation idempotent.
    pub fn upsert(&mut self, key: u64, vector: &[f32]) -> Result<()> {
        // Ignore remove's outcome: key-not-present is fine, we just want
        // the post-condition "key no longer in index" before add().
        let _ = self.index.remove(key);
        self.add(key, vector)
    }

    /// Insert a (key, vector) pair.
    ///
    /// Returns an error if `vector.len() != self.dim`. Fails on duplicate
    /// keys — use [`Self::upsert`] for last-write-wins semantics.
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

// ---------------------------------------------------------------------------
// DebouncedSaver
// ---------------------------------------------------------------------------

/// Batches many `mark_dirty` signals into at most one `UsearchIndex::save`
/// per `debounce` window.
///
/// Usage: pair a `DebouncedSaver` with each cached index. Every mutation
/// (`upsert` / `add`) on the index calls [`DebouncedSaver::mark_dirty`];
/// a background task listens on the channel, sleeps `debounce`, then (if
/// the dirty flag is still set) takes a read lock on the index and saves
/// to disk. [`DebouncedSaver::flush`] forces a synchronous save for
/// graceful shutdown.
///
/// The dirty flag is a single [`AtomicBool`]: many `mark_dirty` calls in
/// a row collapse to one save. The channel is `Sender<()>` with capacity
/// 1 — the `try_send` path silently drops when a wake-up is already in
/// flight, which is exactly what we want (one signal is enough).
pub struct DebouncedSaver {
    dirty: Arc<AtomicBool>,
    save_tx: mpsc::Sender<()>,
    index: Arc<RwLock<UsearchIndex>>,
    path: PathBuf,
}

impl DebouncedSaver {
    /// Spawn the background task and return a handle for `mark_dirty` /
    /// `flush`. The task runs until `cancel` fires; it does a final save
    /// on shutdown if the dirty flag is set.
    pub fn new(
        index: Arc<RwLock<UsearchIndex>>,
        path: PathBuf,
        debounce: Duration,
        cancel: CancellationToken,
    ) -> Self {
        let (save_tx, mut save_rx) = mpsc::channel::<()>(1);
        let dirty = Arc::new(AtomicBool::new(false));

        let this = Self {
            dirty: dirty.clone(),
            save_tx,
            index: index.clone(),
            path: path.clone(),
        };

        let bg_dirty = dirty;
        let bg_index = index;
        let bg_path = path;
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => {
                        // Final save on shutdown so no dirty state is lost.
                        if bg_dirty.swap(false, Ordering::AcqRel) {
                            if let Err(e) = save_now(&bg_index, &bg_path).await {
                                tracing::warn!(
                                    "DebouncedSaver shutdown save failed: {e}"
                                );
                            }
                        }
                        break;
                    }
                    recv = save_rx.recv() => {
                        if recv.is_none() {
                            // All senders dropped → no one can mark dirty
                            // again, so exit the loop.
                            break;
                        }
                        // Coalesce: sleep the debounce window. Further
                        // mark_dirty calls just re-set the flag (try_send
                        // on a full channel is a no-op), so this one
                        // sleep covers them all.
                        tokio::select! {
                            _ = cancel.cancelled() => {
                                if bg_dirty.swap(false, Ordering::AcqRel) {
                                    let _ = save_now(&bg_index, &bg_path).await;
                                }
                                break;
                            }
                            _ = tokio::time::sleep(debounce) => {}
                        }
                        if bg_dirty.swap(false, Ordering::AcqRel) {
                            if let Err(e) = save_now(&bg_index, &bg_path).await {
                                tracing::warn!(
                                    "DebouncedSaver batched save failed: {e}"
                                );
                            }
                        }
                    }
                }
            }
        });

        this
    }

    /// Mark the index dirty and (at most once per debounce window) wake
    /// the background task to schedule a save.
    pub fn mark_dirty(&self) {
        self.dirty.store(true, Ordering::Release);
        // Channel has capacity 1 — silently drop when a wake-up is
        // already queued; the flag is what actually carries state.
        let _ = self.save_tx.try_send(());
    }

    /// Force a synchronous save right now (clears the dirty flag).
    pub async fn flush(&self) -> Result<()> {
        self.dirty.store(false, Ordering::Release);
        save_now(&self.index, &self.path).await
    }
}

impl std::fmt::Debug for DebouncedSaver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DebouncedSaver")
            .field("path", &self.path)
            .field("dirty", &self.dirty.load(Ordering::Acquire))
            .finish()
    }
}

async fn save_now(index: &Arc<RwLock<UsearchIndex>>, path: &Path) -> Result<()> {
    let guard = index.read().await;
    guard.save(path)
}

// ---------------------------------------------------------------------------
// IndexCache (LRU with idle-TTL unload)
// ---------------------------------------------------------------------------

/// One cached `.usearch` index plus its debounced saver and last-used
/// timestamp.
struct CachedIndex {
    index: Arc<RwLock<UsearchIndex>>,
    last_used: Arc<RwLock<Instant>>,
    saver: Arc<DebouncedSaver>,
    /// Per-entry cancellation: dropping the cache or evicting an entry
    /// shuts down its saver task cleanly.
    saver_cancel: CancellationToken,
}

impl CachedIndex {
    async fn touch(&self) {
        *self.last_used.write().await = Instant::now();
    }
}

/// LRU cache of `.usearch` indexes with idle-TTL eviction.
///
/// - `get(name, path, dim)` returns an already-loaded index or lazily
///   `open_checked(path, dim)`s it, and touches the last-used timestamp.
/// - `insert(name, index, path)` registers a freshly-built index (used
///   by ingest paths where we have the index in hand).
/// - [`start_sweeper`] spawns a background task that periodically scans
///   every entry and evicts (after saving, if dirty) anything idle
///   longer than `ttl`. This is the "LRU unload" mechanism — strictly
///   TTL-based, not capacity-based: memory usage is bounded by "how
///   many distinct indexes were touched in the last `ttl`".
///
/// Default save-debounce for entries inserted through this cache is
/// [`Self::DEFAULT_SAVE_DEBOUNCE`]. Callers that need a different
/// window should build their own `DebouncedSaver` alongside.
pub struct IndexCache {
    entries: DashMap<String, CachedIndex>,
    ttl: Duration,
    sweep_interval: Duration,
    save_debounce: Duration,
}

impl IndexCache {
    /// Default idle-TTL before an index is unloaded (2 hours — matches
    /// the legacy `KNOWLEDGEBASE_INDEX_IDLE_TTL_MS` setting).
    pub const DEFAULT_TTL: Duration = Duration::from_secs(2 * 60 * 60);
    /// Default sweep interval (10 minutes).
    pub const DEFAULT_SWEEP: Duration = Duration::from_secs(10 * 60);
    /// Default save debounce window (200 ms — one "burst of edits"
    /// collapses into a single save).
    pub const DEFAULT_SAVE_DEBOUNCE: Duration = Duration::from_millis(200);

    /// Build a cache with explicit timers. Use `IndexCache::default()`
    /// for the production 2h/10min/200ms defaults.
    pub fn new(ttl: Duration, sweep_interval: Duration) -> Self {
        Self {
            entries: DashMap::new(),
            ttl,
            sweep_interval,
            save_debounce: Self::DEFAULT_SAVE_DEBOUNCE,
        }
    }

    /// Tune the save-debounce window for entries inserted after this
    /// call. Useful in tests that want to observe save batching quickly.
    pub fn with_save_debounce(mut self, debounce: Duration) -> Self {
        self.save_debounce = debounce;
        self
    }

    /// Get-or-load; touches `last_used` on every call.
    ///
    /// If `name` isn't cached, `open_checked(path, dim)` is called and
    /// the result is inserted before returning.
    pub async fn get(
        &self,
        name: &str,
        path: &Path,
        dim: usize,
    ) -> Result<Arc<RwLock<UsearchIndex>>> {
        if let Some(entry) = self.entries.get(name) {
            entry.touch().await;
            return Ok(entry.index.clone());
        }
        let index = UsearchIndex::open_checked(path, dim)
            .map_err(|e| anyhow!("IndexCache::get: load {name}: {e}"))?;
        self.insert(name.to_string(), index, path.to_path_buf())
            .await;
        // Unwrap is safe: we just inserted it under the same key.
        let entry = self
            .entries
            .get(name)
            .ok_or_else(|| anyhow!("IndexCache::get: race on insert {name}"))?;
        Ok(entry.index.clone())
    }

    /// Insert an already-loaded index; if a previous entry exists under
    /// `name`, its background saver is cancelled and replaced.
    pub async fn insert(&self, name: String, index: UsearchIndex, path: PathBuf) {
        // Cancel any prior saver for this slot.
        if let Some((_, old)) = self.entries.remove(&name) {
            old.saver_cancel.cancel();
        }
        let index = Arc::new(RwLock::new(index));
        let cancel = CancellationToken::new();
        let saver = Arc::new(DebouncedSaver::new(
            index.clone(),
            path.clone(),
            self.save_debounce,
            cancel.clone(),
        ));
        let entry = CachedIndex {
            index,
            last_used: Arc::new(RwLock::new(Instant::now())),
            saver,
            saver_cancel: cancel,
        };
        // `path` is captured into the saver; the cache itself doesn't
        // need a separate copy.
        let _ = path;
        self.entries.insert(name, entry);
    }

    /// Get the [`DebouncedSaver`] for an entry — callers use this to
    /// `mark_dirty` after mutating the underlying index.
    pub fn saver(&self, name: &str) -> Option<Arc<DebouncedSaver>> {
        self.entries.get(name).map(|e| e.saver.clone())
    }

    /// Evict an entry, flushing any pending dirty state first. Returns
    /// `true` if something was removed.
    pub async fn evict(&self, name: &str) -> bool {
        let Some((_, entry)) = self.entries.remove(name) else {
            return false;
        };
        // Flush synchronously so no dirty state is lost on eviction.
        if let Err(e) = entry.saver.flush().await {
            tracing::warn!("IndexCache::evict: flush {name} failed: {e}");
        }
        entry.saver_cancel.cancel();
        true
    }

    /// Number of currently-cached indexes.
    pub fn size(&self) -> usize {
        self.entries.len()
    }

    /// Spawn the idle-sweeper background task.
    ///
    /// Every `sweep_interval`, scans all entries; any entry whose
    /// `last_used` is older than `ttl` is flushed + evicted.
    pub fn start_sweeper(
        self: Arc<Self>,
        cancel: CancellationToken,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    _ = tokio::time::sleep(self.sweep_interval) => {
                        self.sweep_once().await;
                    }
                }
            }
        })
    }

    /// Single sweep pass — flush+evict every entry whose idle time
    /// exceeds `ttl`. Public for tests; production callers go through
    /// [`start_sweeper`].
    pub async fn sweep_once(&self) {
        let now = Instant::now();
        // Snapshot the (name, last_used) pairs so we don't hold DashMap
        // shards across `.await` inside eviction.
        let mut stale: Vec<String> = Vec::new();
        for entry in self.entries.iter() {
            let last_used = *entry.last_used.read().await;
            if now.saturating_duration_since(last_used) > self.ttl {
                stale.push(entry.key().clone());
            }
        }
        for name in stale {
            self.evict(&name).await;
        }
    }
}

impl Default for IndexCache {
    fn default() -> Self {
        Self::new(Self::DEFAULT_TTL, Self::DEFAULT_SWEEP)
    }
}

impl std::fmt::Debug for IndexCache {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("IndexCache")
            .field("size", &self.entries.len())
            .field("ttl", &self.ttl)
            .field("sweep_interval", &self.sweep_interval)
            .field("save_debounce", &self.save_debounce)
            .finish()
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

    // -----------------------------------------------------------------
    // Sprint 3 T1+T3: upsert, DebouncedSaver, IndexCache
    // -----------------------------------------------------------------

    #[test]
    fn upsert_existing_key_replaces_vector() {
        let mut idx = UsearchIndex::create_with_capacity(4, 16).unwrap();
        idx.upsert(7, &[1.0, 0.0, 0.0, 0.0]).unwrap();
        idx.upsert(7, &[0.0, 1.0, 0.0, 0.0]).unwrap();
        // Size stays at 1 — second upsert replaces rather than appending.
        assert_eq!(idx.size(), 1);
        // Nearest to [0,1,0,0] must be key 7, and the distance should
        // reflect the *new* vector (exact match ≈ 0), not the original.
        let hits = idx.search(&[0.0, 1.0, 0.0, 0.0], 1).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, 7);
        assert!(hits[0].1 < 1e-3, "dist={}", hits[0].1);
    }

    #[test]
    fn upsert_new_key_adds() {
        // upsert on a missing key must be a plain add (remove is a no-op).
        let mut idx = UsearchIndex::create_with_capacity(3, 8).unwrap();
        idx.upsert(101, &[0.5, 0.5, 0.5]).unwrap();
        assert_eq!(idx.size(), 1);
        let hits = idx.search(&[0.5, 0.5, 0.5], 1).unwrap();
        assert_eq!(hits[0].0, 101);
    }

    #[tokio::test]
    async fn debounced_saver_batches_multiple_dirties() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("debounced.usearch");

        // Seed a file so mtime starts meaningful.
        let mut seed = UsearchIndex::create_with_capacity(2, 4).unwrap();
        seed.add(1, &[1.0, 0.0]).unwrap();
        seed.save(&path).unwrap();
        let index = Arc::new(RwLock::new(seed));

        let cancel = CancellationToken::new();
        let saver = DebouncedSaver::new(
            index.clone(),
            path.clone(),
            Duration::from_millis(100),
            cancel.clone(),
        );

        let mtime_before = std::fs::metadata(&path).unwrap().modified().unwrap();
        // Give the filesystem enough resolution that "same mtime" means
        // "no save happened" (HFS+ only stores whole seconds on older
        // macs; APFS is ns but we sleep past 1s to be safe).
        tokio::time::sleep(Duration::from_millis(1100)).await;

        // Burst of 5 mark_dirty within the debounce window.
        for _ in 0..5 {
            saver.mark_dirty();
        }
        // Wait long enough for the 100ms debounce to fire once.
        tokio::time::sleep(Duration::from_millis(400)).await;

        let mtime_after = std::fs::metadata(&path).unwrap().modified().unwrap();
        assert!(
            mtime_after > mtime_before,
            "expected one save after burst; mtime unchanged"
        );

        // Another 300ms of idle must *not* produce a new save (flag is
        // clear, channel is empty).
        let mtime_settled = std::fs::metadata(&path).unwrap().modified().unwrap();
        tokio::time::sleep(Duration::from_millis(300)).await;
        let mtime_final = std::fs::metadata(&path).unwrap().modified().unwrap();
        assert_eq!(
            mtime_final, mtime_settled,
            "no further save should happen while flag is clean"
        );

        cancel.cancel();
    }

    #[tokio::test]
    async fn debounced_saver_flush_saves_immediately() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("flush.usearch");

        let mut seed = UsearchIndex::create_with_capacity(2, 4).unwrap();
        seed.add(1, &[1.0, 0.0]).unwrap();
        seed.save(&path).unwrap();
        let index = Arc::new(RwLock::new(seed));

        let cancel = CancellationToken::new();
        // Large debounce so the background task wouldn't fire before
        // flush does.
        let saver = DebouncedSaver::new(
            index.clone(),
            path.clone(),
            Duration::from_secs(10),
            cancel.clone(),
        );

        // Mutate under write lock; flush must persist the new state.
        index.write().await.upsert(2, &[0.0, 1.0]).unwrap();
        saver.mark_dirty();

        saver.flush().await.unwrap();

        let reloaded = UsearchIndex::open(&path).unwrap();
        assert_eq!(reloaded.size(), 2);

        cancel.cancel();
    }

    #[tokio::test]
    async fn index_cache_get_touches_last_used() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("cache_touch.usearch");

        // Seed a saved index on disk for IndexCache::get to load.
        {
            let mut idx = UsearchIndex::create_with_capacity(3, 4).unwrap();
            idx.add(1, &[1.0, 0.0, 0.0]).unwrap();
            idx.save(&path).unwrap();
        }

        let cache = IndexCache::new(Duration::from_secs(3600), Duration::from_secs(3600));
        let _ = cache.get("kb", &path, 3).await.unwrap();

        // Grab the first timestamp. Clone the Arc out of the DashMap
        // guard so we don't hold a shard lock across the `.await`.
        let last_used_handle = cache
            .entries
            .get("kb")
            .map(|e| e.last_used.clone())
            .unwrap();
        let t0 = *last_used_handle.read().await;

        // Ensure enough time elapses to observe a monotonic bump.
        tokio::time::sleep(Duration::from_millis(20)).await;

        let _ = cache.get("kb", &path, 3).await.unwrap();
        let t1 = *last_used_handle.read().await;

        assert!(t1 > t0, "last_used should advance on hit");
        assert_eq!(cache.size(), 1, "no duplicate entry on hit");
    }

    #[tokio::test]
    async fn index_cache_sweeper_evicts_stale() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("cache_evict.usearch");
        {
            let mut idx = UsearchIndex::create_with_capacity(2, 4).unwrap();
            idx.add(1, &[1.0, 0.0]).unwrap();
            idx.save(&path).unwrap();
        }

        // TTL 50ms, sweep 20ms: a single get() then a short wait evicts.
        let cache = Arc::new(IndexCache::new(
            Duration::from_millis(50),
            Duration::from_millis(20),
        ));
        let _ = cache.get("kb", &path, 2).await.unwrap();
        assert_eq!(cache.size(), 1);

        let cancel = CancellationToken::new();
        let handle = cache.clone().start_sweeper(cancel.clone());

        // Wait past TTL plus a full sweep interval.
        tokio::time::sleep(Duration::from_millis(150)).await;
        assert_eq!(cache.size(), 0, "stale entry should be evicted");

        cancel.cancel();
        let _ = handle.await;
    }

    #[tokio::test]
    async fn index_cache_sweeper_saves_dirty_before_evict() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("cache_dirty.usearch");
        {
            let mut idx = UsearchIndex::create_with_capacity(2, 4).unwrap();
            idx.add(1, &[1.0, 0.0]).unwrap();
            idx.save(&path).unwrap();
        }

        let cache = Arc::new(
            IndexCache::new(Duration::from_millis(50), Duration::from_millis(20))
                // Keep the debouncer slow so `evict`'s `flush` is the
                // thing that ends up persisting the change — this is
                // what we're testing.
                .with_save_debounce(Duration::from_secs(10)),
        );
        let handle = cache.get("kb", &path, 2).await.unwrap();

        // Dirty the index: add key 2 then mark the saver dirty.
        handle.write().await.upsert(2, &[0.0, 1.0]).unwrap();
        cache.saver("kb").unwrap().mark_dirty();

        // Sanity: on-disk file still only has 1 entry.
        assert_eq!(UsearchIndex::open(&path).unwrap().size(), 1);

        let cancel = CancellationToken::new();
        let sweeper = cache.clone().start_sweeper(cancel.clone());

        // Wait past TTL + sweep cycle; the sweeper must flush dirty
        // state to disk before dropping the entry.
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(cache.size(), 0, "stale entry should be evicted");

        // The freshly-reloaded file must see the upsert from before the
        // evict — i.e. `evict` did call `flush` synchronously.
        let reloaded = UsearchIndex::open(&path).unwrap();
        assert_eq!(reloaded.size(), 2, "dirty state should be flushed on evict");

        cancel.cancel();
        let _ = sweeper.await;
    }
}
