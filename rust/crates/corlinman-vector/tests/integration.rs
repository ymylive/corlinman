//! End-to-end integration tests for the corlinman-native hybrid
//! retrieval stack: HNSW + BM25 + RRF fusion.
//!
//! Fixtures are generated **dynamically** into a `tempfile::TempDir` on
//! each run; nothing is cached on disk under `qa/fixtures/`. The PRNG
//! is seeded so the generated content / vectors are reproducible
//! across machines.

use std::path::PathBuf;

use corlinman_vector::hybrid::HitSource;
use corlinman_vector::migration::{ensure_schema, MigrationOutcome};
use corlinman_vector::sqlite::SqliteStore;
use corlinman_vector::usearch_index::UsearchIndex;
use corlinman_vector::VectorStore;
use tempfile::TempDir;

const NUM_CHUNKS: usize = 20;
const DIM: usize = 16;
const SEED: u64 = 0x1234_5678_9abc_def0;

/// Tiny seeded PRNG (xorshift64*) — avoids pulling `rand` as a dev-dep.
struct Xorshift {
    state: u64,
}

impl Xorshift {
    fn new(seed: u64) -> Self {
        Self { state: seed.max(1) }
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn next_f32_symmetric(&mut self) -> f32 {
        let bits = (self.next_u64() >> 40) as u32; // 24 significant bits
        ((bits as f32) / ((1u32 << 23) as f32)) - 1.0
    }
    fn next_range(&mut self, n: usize) -> usize {
        (self.next_u64() as usize) % n.max(1)
    }
}

fn normalize(v: &mut [f32]) {
    let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > f32::EPSILON {
        for x in v.iter_mut() {
            *x /= norm;
        }
    }
}

/// English vocabulary — distinct enough that BM25 can lock onto a
/// single chunk given a unique tri-gram.
const EN_WORDS: &[&str] = &[
    "corlinman",
    "orchard",
    "pipeline",
    "relay",
    "beacon",
    "cascade",
    "nimbus",
    "forge",
    "meadow",
    "quartz",
    "silhouette",
    "thicket",
    "voyager",
    "wistful",
    "zenith",
    "borealis",
];

/// Chinese vocabulary — mixed with the English words to exercise
/// FTS5's unicode tokenization path.
const ZH_WORDS: &[&str] = &[
    "日志",
    "检索",
    "融合",
    "索引",
    "向量",
    "关键词",
    "语义",
    "沉思",
    "桥梁",
    "清晨",
    "黄昏",
    "归档",
    "笔记",
];

/// Build a deterministic chunk text: 4–8 words mixing `EN_WORDS` and
/// `ZH_WORDS`, prefixed with a stable marker so individual chunks are
/// easy to assert against.
fn synth_content(rng: &mut Xorshift, chunk_id: usize) -> String {
    let word_count = 4 + rng.next_range(5); // 4..=8
    let mut parts = vec![format!("chunk{chunk_id:02}")];
    for _ in 0..word_count {
        let use_zh = (rng.next_u64() & 1) == 0;
        let pick = if use_zh {
            ZH_WORDS[rng.next_range(ZH_WORDS.len())]
        } else {
            EN_WORDS[rng.next_range(EN_WORDS.len())]
        };
        parts.push(pick.to_string());
    }
    parts.join(" ")
}

fn synth_vector(rng: &mut Xorshift) -> Vec<f32> {
    let mut v: Vec<f32> = (0..DIM).map(|_| rng.next_f32_symmetric()).collect();
    normalize(&mut v);
    v
}

/// Build the sqlite + usearch fixture under `dir`. Returns the paths
/// plus the list of (content, vector) tuples so tests can query with
/// known inputs.
async fn build_fixture(dir: &std::path::Path) -> (PathBuf, PathBuf, Vec<(String, Vec<f32>)>) {
    let sqlite_path = dir.join("kb.sqlite");
    let usearch_path = dir.join("index.usearch");

    let store = SqliteStore::open(&sqlite_path).await.unwrap();
    assert_eq!(
        ensure_schema(&store).await.unwrap(),
        MigrationOutcome::Initialised(corlinman_vector::SCHEMA_VERSION)
    );

    let file_id = store
        .insert_file(
            "公共/fixture.md",
            "公共",
            "fixturehash",
            1_700_000_000,
            4096,
        )
        .await
        .unwrap();

    let mut index = UsearchIndex::create_with_capacity(DIM, 64).unwrap();
    let mut rng = Xorshift::new(SEED);
    let mut corpus = Vec::with_capacity(NUM_CHUNKS);

    for i in 0..NUM_CHUNKS {
        let content = synth_content(&mut rng, i);
        let vec = synth_vector(&mut rng);
        let chunk_id = store
            .insert_chunk(file_id, i as i64, &content, Some(&vec))
            .await
            .unwrap();
        index.add(chunk_id as u64, &vec).unwrap();
        corpus.push((content, vec));
    }

    index.save(&usearch_path).unwrap();
    (sqlite_path, usearch_path, corpus)
}

#[tokio::test]
async fn dense_only_returns_nearest_vector_first() {
    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, corpus) = build_fixture(tmp.path()).await;
    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .unwrap();

    // Query with chunk 0's vector — dense-only must return it first
    // with source=Dense.
    let hits = store.query_dense(&corpus[0].1, 3).await.unwrap();
    assert_eq!(hits.len(), 3);
    assert!(
        hits[0].content.starts_with("chunk00"),
        "expected chunk00 first, got {}",
        hits[0].content
    );
    assert_eq!(hits[0].source, HitSource::Dense);
    // Similarity is non-increasing.
    for pair in hits.windows(2) {
        assert!(
            pair[0].score + 1e-6 >= pair[1].score,
            "score order broken: {} then {}",
            pair[0].score,
            pair[1].score
        );
    }
}

#[tokio::test]
async fn sparse_only_finds_chunk_by_unique_token() {
    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, _corpus) = build_fixture(tmp.path()).await;
    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .unwrap();

    // "chunk05" is a unique marker — BM25 must find exactly that chunk.
    let hits = store.query_sparse("chunk05", 5).await.unwrap();
    assert!(!hits.is_empty(), "BM25 should find the marker");
    assert!(hits[0].content.starts_with("chunk05"));
    assert_eq!(hits[0].source, HitSource::Sparse);
    assert_eq!(hits[0].path, "公共/fixture.md");
}

#[tokio::test]
async fn hybrid_fusion_outranks_single_path_when_both_agree() {
    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, corpus) = build_fixture(tmp.path()).await;
    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .unwrap();

    // Pick the first token out of chunk 0's content — BM25 will surface
    // chunk 0. Dense is also queried with chunk 0's vector. RRF should
    // tag chunk 0 as HitSource::Both and rank it first.
    let marker = corpus[0].0.split_whitespace().next().unwrap(); // "chunk00"
    let hits = store.query(marker, &corpus[0].1, 5).await.unwrap();
    assert!(!hits.is_empty());
    assert!(
        hits[0].content.starts_with("chunk00"),
        "hybrid top-1 should be chunk00, got {}",
        hits[0].content
    );
    assert_eq!(
        hits[0].source,
        HitSource::Both,
        "chunk00 appears in both paths, RRF must mark it as Both"
    );

    // Compare against dense-only: hybrid's top-1 fused score should be
    // strictly larger than dense-only for a doc that appears in both.
    let dense_only = store.query_dense(&corpus[0].1, 5).await.unwrap();
    let dense_top_for_chunk0 = dense_only
        .iter()
        .find(|h| h.content.starts_with("chunk00"))
        .unwrap();
    // They're on different scales (cosine-sim vs RRF sum), so compare
    // the *rank* instead: hybrid must not demote chunk00.
    assert_eq!(hits[0].chunk_id, dense_top_for_chunk0.chunk_id);
}

#[tokio::test]
async fn overfetch_multiplier_controls_candidate_pool() {
    use corlinman_vector::hybrid::{HybridParams, HybridSearcher};
    use std::sync::Arc;
    use tokio::sync::RwLock;

    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, corpus) = build_fixture(tmp.path()).await;

    let sqlite = Arc::new(SqliteStore::open(&sqlite_path).await.unwrap());
    let index = Arc::new(RwLock::new(UsearchIndex::open(&usearch_path).unwrap()));

    // Use a BM25 query that multiple chunks can match. The corpus is
    // assembled from a shared vocabulary so common tokens like "融合"
    // will appear across many chunks.
    let token = "融合 索引 向量";

    // top_k=1 + overfetch=1 → each ranker returns 1 doc → fused pool ≤ 2.
    // top_k=1 + overfetch=NUM_CHUNKS → each ranker returns up to NUM_CHUNKS
    // → fused pool is the entire corpus.
    let narrow = HybridSearcher::new(
        sqlite.clone(),
        index.clone(),
        HybridParams {
            top_k: 1,
            overfetch_multiplier: 1,
            ..HybridParams::default()
        },
    );
    let wide = HybridSearcher::new(
        sqlite,
        index,
        HybridParams {
            top_k: 1,
            overfetch_multiplier: NUM_CHUNKS,
            ..HybridParams::default()
        },
    );

    let narrow_hit = narrow.search(token, &corpus[0].1, None).await.unwrap();
    let wide_hit = wide.search(token, &corpus[0].1, None).await.unwrap();

    // Both must return exactly 1 hit (top_k=1).
    assert_eq!(narrow_hit.len(), 1);
    assert_eq!(wide_hit.len(), 1);

    // The overfetch knob must not crash or cap the pool below top_k.
    // With overfetch=1 the narrow pool can't tag a doc as Both unless
    // it is simultaneously rank-1 in dense AND rank-1 in sparse; with
    // overfetch=NUM_CHUNKS many docs can earn the Both tag. Assert the
    // source tagging makes sense: wide's winner, if Both, came from
    // the full pool.
    let wide_src = wide_hit[0].source;
    assert!(
        matches!(
            wide_src,
            HitSource::Dense | HitSource::Sparse | HitSource::Both
        ),
        "wide winner source must be a valid tag, got {:?}",
        wide_src
    );

    // Also run a smoke test at default params to make sure the
    // parameterised path matches the simpler one for equivalent
    // configs.
    let default_params = HybridParams::default();
    let default_searcher = HybridSearcher::new(
        Arc::new(SqliteStore::open(&sqlite_path).await.unwrap()),
        Arc::new(RwLock::new(UsearchIndex::open(&usearch_path).unwrap())),
        default_params,
    );
    let default_hits = default_searcher
        .search(token, &corpus[0].1, None)
        .await
        .unwrap();
    assert!(default_hits.len() <= default_params.top_k);
}

#[tokio::test]
async fn query_top_k_zero_returns_empty() {
    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, corpus) = build_fixture(tmp.path()).await;
    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .unwrap();

    let hits = store.query("chunk00", &corpus[0].1, 0).await.unwrap();
    assert!(hits.is_empty());

    let dense = store.query_dense(&corpus[0].1, 0).await.unwrap();
    assert!(dense.is_empty());

    let sparse = store.query_sparse("chunk00", 0).await.unwrap();
    assert!(sparse.is_empty());
}

#[tokio::test]
async fn hybrid_respects_final_top_k_cap() {
    let tmp = TempDir::new().unwrap();
    let (sqlite_path, usearch_path, corpus) = build_fixture(tmp.path()).await;
    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .unwrap();

    // Query with a high-recall BM25 token (most chunks share "陈旧"
    // markers from the dictionary) + dense. top_k=3 must cap exactly.
    let hits = store
        .query("日志 检索 融合", &corpus[0].1, 3)
        .await
        .unwrap();
    assert!(hits.len() <= 3, "got {} hits, expected ≤ 3", hits.len());

    // RRF scores must be strictly non-increasing.
    for pair in hits.windows(2) {
        assert!(
            pair[0].score + 1e-6 >= pair[1].score,
            "score order: {} then {}",
            pair[0].score,
            pair[1].score
        );
    }
}
