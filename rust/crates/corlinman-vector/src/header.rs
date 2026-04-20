//! Read-only metadata probe for `.usearch` index files.
//!
//! Used by [`crate::migration::probe_and_convert_if_needed`] to detect
//! embedding-dimension drift between the live DB and an on-disk HNSW
//! index (e.g. after the embedding model is swapped). The format version
//! field is reserved for a future conversion hook — usearch 2.x keeps
//! metadata in the file header but doesn't expose a version string
//! through the Rust crate, so we record what we can read today and
//! leave an explicit TODO for the cross-version conversion path.

use std::path::Path;

use corlinman_core::error::CorlinmanError;
use usearch::{ffi::IndexOptions, Index, MetricKind, ScalarKind};

/// Metadata scraped from a usearch file header without fully adopting
/// the index into the HNSW hot path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsearchHeader {
    /// Embedding dimensionality recorded in the file.
    pub dim: usize,
    /// usearch crate format marker. The Rust binding does not expose a
    /// format-version string, so we stamp this with the crate's semver
    /// at build time as a best-effort compatibility record.
    pub version: String,
    /// Number of vectors present in the index.
    pub count: usize,
}

/// Load a `.usearch` file's header metadata.
///
/// Internally this constructs a scratch `Index` with `dimensions=1` and
/// calls `load()`; usearch adopts the file's own header values, so we
/// read `dimensions()` and `size()` back off the adopted index. The
/// returned `UsearchHeader` does **not** retain the loaded index — the
/// scratch instance is dropped at function exit.
pub fn probe_usearch_header(path: &Path) -> Result<UsearchHeader, CorlinmanError> {
    let opts = IndexOptions {
        dimensions: 1,
        metric: MetricKind::Cos,
        quantization: ScalarKind::F32,
        connectivity: 0,
        expansion_add: 0,
        expansion_search: 0,
        multi: false,
    };
    let index = Index::new(&opts).map_err(|e| {
        CorlinmanError::Storage(format!("usearch Index::new for probe failed: {e}"))
    })?;
    let path_str = path
        .to_str()
        .ok_or_else(|| CorlinmanError::Config(format!("non-UTF8 path: {}", path.display())))?;
    index.load(path_str).map_err(|e| {
        CorlinmanError::Storage(format!(
            "usearch load({}) failed during probe: {e}",
            path.display()
        ))
    })?;
    let dim = index.dimensions();
    if dim == 0 {
        return Err(CorlinmanError::Storage(format!(
            "usearch header reports dim=0 ({}) — corrupt file?",
            path.display()
        )));
    }
    Ok(UsearchHeader {
        dim,
        version: env!("CARGO_PKG_VERSION").to_string(),
        count: index.size(),
    })
}

/// Inspect the on-disk index and fail loudly if its dimensionality
/// disagrees with the caller's expectation.
///
/// - Missing file → `Ok(())` (fresh install; the caller will build a
///   new index the first time it saves).
/// - Matching dim → `Ok(())`.
/// - Mismatched dim → [`CorlinmanError::Config`] telling the operator
///   to rebuild.
///
/// Format-version conversion is reserved for a later sprint; the header
/// already carries the field but usearch 2.x doesn't expose a file
/// format marker for us to act on, so we stub it.
pub fn probe_and_convert_if_needed(
    index_path: &Path,
    expected_dim: usize,
) -> Result<(), CorlinmanError> {
    if !index_path.exists() {
        return Ok(());
    }
    let header = probe_usearch_header(index_path)?;
    if header.dim != expected_dim {
        return Err(CorlinmanError::Config(format!(
            "usearch dim mismatch at {}: file={} expected={}; rebuild the HNSW index",
            index_path.display(),
            header.dim,
            expected_dim
        )));
    }
    // TODO(S4): call into a version-conversion hook when usearch-rs
    // surfaces a binary format version we can branch on. For now every
    // file that loads at all is considered compatible.
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::usearch_index::UsearchIndex;
    use tempfile::TempDir;

    #[test]
    fn probe_reads_dim_and_count() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("probe.usearch");
        {
            let mut idx = UsearchIndex::create_with_capacity(8, 4).unwrap();
            idx.add(1, &[0.0_f32; 8]).unwrap();
            idx.add(2, &[1.0_f32; 8]).unwrap();
            idx.save(&path).unwrap();
        }
        let h = probe_usearch_header(&path).unwrap();
        assert_eq!(h.dim, 8);
        assert_eq!(h.count, 2);
        assert!(!h.version.is_empty());
    }

    #[test]
    fn probe_and_convert_matching_dim_is_ok() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("ok.usearch");
        {
            let idx = UsearchIndex::create_with_capacity(4, 4).unwrap();
            idx.save(&path).unwrap();
        }
        probe_and_convert_if_needed(&path, 4).unwrap();
    }

    #[test]
    fn probe_and_convert_dim_mismatch_returns_config_err() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("mismatch.usearch");
        {
            let idx = UsearchIndex::create_with_capacity(4, 4).unwrap();
            idx.save(&path).unwrap();
        }
        let err = probe_and_convert_if_needed(&path, 5).unwrap_err();
        assert!(matches!(err, CorlinmanError::Config(_)));
        assert!(err.to_string().contains("dim mismatch"));
    }

    #[test]
    fn probe_and_convert_missing_file_is_noop() {
        let tmp = TempDir::new().unwrap();
        probe_and_convert_if_needed(&tmp.path().join("nope.usearch"), 8).unwrap();
    }
}
