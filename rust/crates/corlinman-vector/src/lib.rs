//! corlinman-vector — RAG persistence + hybrid retrieval.
//!
//! Native corlinman RAG is a three-part pipeline:
//!
//! 1. **HNSW dense recall** via [`usearch_index::UsearchIndex`]
//!    (usearch 2.x, cosine metric).
//! 2. **BM25 sparse recall** via [`sqlite::SqliteStore::search_bm25`]
//!    (SQLite FTS5 `bm25()` over `chunks.content`).
//! 3. **RRF fusion** via [`hybrid::HybridSearcher`]
//!    (reciprocal-rank fusion with per-ranker weights).
//!
//! Cross-encoder rerank ships as a pluggable [`rerank::Reranker`] trait
//! (Sprint 3 T6); the default is [`rerank::NoopReranker`] and the gRPC
//! path to the Python embedding service is stubbed in
//! [`rerank::GrpcReranker`]. LRU unload of `.usearch` files is the
//! remaining roadmap item. Tag-filter pushdown landed with Sprint 3 T4
//! via [`hybrid::TagFilter`] + the `chunk_tags` table (see
//! [`sqlite::SCHEMA_SQL`]).
//!
//! ## Module layout
//!
//! - [`sqlite`]: sqlx pool + FTS5 MATCH helper.
//! - [`usearch_index`]: HNSW wrapper.
//! - [`hybrid`]: RRF fusion over dense + sparse result sets.
//! - [`query::VectorStore`]: the public facade combining both.
//! - [`migration`]: `kv_store('schema_version')` bootstrap + trait-based
//!   migration registry (Sprint 3 T2).
//! - [`header`]: read-only `.usearch` header probe used by the migration
//!   runner to detect embedding-dimension drift.

pub mod decay;
pub mod header;
pub mod hybrid;
pub mod migration;
pub mod query;
pub mod rerank;
pub mod sqlite;
pub mod usearch_index;

pub use decay::{apply_decay, boosted_score, DecayConfig, CONSOLIDATED_NAMESPACE};
pub use header::{probe_and_convert_if_needed, probe_usearch_header, UsearchHeader};
pub use hybrid::{
    CandidateBoost, EpaBoost, HitSource, HybridParams, HybridSearcher, RagHit, TagFilter,
};
pub use migration::{
    MigrationRegistry, MigrationReport, MigrationScript, V1ToV2FtsBackfill, V2ToV3PendingApprovals,
    V3ToV4ChunkTags, V4ToV5ChunksNamespace, V5ToV6TagNodesAndEpa,
};
pub use query::VectorStore;
pub use rerank::{GrpcReranker, NoopReranker, Reranker};
pub use sqlite::{
    ChunkDecayState, ChunkEpaRow, ChunkRow, FileRow, PendingApproval, SqliteStore, TagNodeRow,
};
pub use usearch_index::UsearchIndex;

/// Current corlinman schema version written to `kv_store('schema_version')`.
///
/// - v1: `files`/`chunks`/`kv_store` baseline (no FTS5).
/// - v2: add FTS5 virtual table `chunks_fts` + sync triggers, plus a
///   one-shot backfill via `rebuild_fts` for pre-existing chunks.
/// - v3: add `pending_approvals` table used by the gateway's tool-approval
///   gate (Sprint 2 T3). Forward-only migration — the DDL is `IF NOT EXISTS`
///   so a fresh v3 DB materialises the table during `SqliteStore::open`, and
///   `migration::ensure_schema` just bumps the stored version for legacy DBs.
/// - v4: add `chunk_tags` (chunk_id, tag) many-to-many + `idx_chunk_tags_tag`
///   supporting the Sprint 3 T4 tag-filter pushdown. The DDL is in
///   [`sqlite::SCHEMA_SQL`] so fresh DBs materialise the table during open;
///   legacy v3 DBs get the table via the [`migration::V3ToV4ChunkTags`]
///   script.
/// - v5: add `chunks.namespace TEXT NOT NULL DEFAULT 'general'` +
///   `idx_chunks_namespace(namespace, id)` for the Sprint 9 T1 memory
///   substrate. Fresh DBs pick up the column via `SCHEMA_SQL`; legacy v4
///   files get it via [`migration::V4ToV5ChunksNamespace`], which is
///   idempotent (ALTER only runs when the column is absent) and reversible
///   (rollback rebuilds `chunks` without the column and re-hooks FTS).
/// - v6: add hierarchical `tag_nodes` tree + retarget `chunk_tags` from
///   `(chunk_id, tag TEXT)` to `(chunk_id, tag_node_id)`, plus an
///   initially-empty `chunk_epa` per-chunk cache (packed projections
///   BLOB + entropy + logic_depth) for the TagMemo engine (Sprint 9
///   T-B3-BE3/BE4). Fresh DBs materialise all three via `SCHEMA_SQL`;
///   legacy v5 files are migrated by [`migration::V5ToV6TagNodesAndEpa`],
///   which seeds one depth-0 node per distinct legacy tag and rewrites
///   `chunk_tags` idempotently.
///
/// Bumped on any breaking migration; see [`migration::ensure_schema`].
pub const SCHEMA_VERSION: i64 = 6;

/// Encode a `&[f32]` as a little-endian byte blob for the `chunks.vector`
/// column.
pub fn f32_slice_to_blob(v: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(v.len() * 4);
    for x in v {
        out.extend_from_slice(&x.to_le_bytes());
    }
    out
}

/// Decode a little-endian f32 BLOB back to `Vec<f32>`.
///
/// Returns `None` if `bytes.len()` is not a multiple of 4.
pub fn blob_to_f32_vec(bytes: &[u8]) -> Option<Vec<f32>> {
    if bytes.len() % 4 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4);
    for chunk in bytes.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Some(out)
}

#[cfg(test)]
mod roundtrip_tests {
    use super::*;

    #[test]
    fn f32_blob_roundtrip() {
        let v = vec![1.0_f32, -2.5, 42.125, 0.0, f32::MIN_POSITIVE];
        let blob = f32_slice_to_blob(&v);
        assert_eq!(blob.len(), v.len() * 4);
        let back = blob_to_f32_vec(&blob).expect("even length");
        assert_eq!(back, v);
    }

    #[test]
    fn blob_wrong_length_rejected() {
        assert!(blob_to_f32_vec(&[1, 2, 3]).is_none());
    }
}
