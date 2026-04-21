//! `kind: rag_hybrid` — populates an in-memory [`SqliteStore`] + usearch
//! index from the YAML corpus, runs [`HybridSearcher::search`], and asserts
//! the top hits.

use std::sync::Arc;

use corlinman_vector::{HybridParams, HybridSearcher, SqliteStore, UsearchIndex};
use tokio::sync::RwLock;

use crate::cmd::qa::scenario::RagHybridScenario;

pub async fn run(sc: &RagHybridScenario) -> anyhow::Result<()> {
    if sc.corpus.is_empty() {
        anyhow::bail!("rag_hybrid scenario has an empty corpus");
    }
    let dim = sc.corpus[0].vector.len();
    if dim == 0 {
        anyhow::bail!("rag_hybrid corpus vectors must have non-zero dim");
    }
    if sc.query.vector.len() != dim {
        anyhow::bail!(
            "query vector dim {} != corpus dim {}",
            sc.query.vector.len(),
            dim
        );
    }
    for (i, c) in sc.corpus.iter().enumerate() {
        if c.vector.len() != dim {
            anyhow::bail!("corpus entry {i} has dim {} != {dim}", c.vector.len());
        }
    }

    // In-memory SQLite file in a tempdir (sqlx's in-memory URL is awkward
    // across connections in a pool).
    let tmp = tempfile::tempdir()?;
    let sqlite_path = tmp.path().join("rag.sqlite");
    let store = Arc::new(SqliteStore::open(&sqlite_path).await?);

    let file_id = store
        .insert_file("qa://scenario.md", "default", "qa-checksum", 0, 0)
        .await?;
    let mut usearch = UsearchIndex::create_with_capacity(dim, sc.corpus.len().max(4))?;

    for (i, entry) in sc.corpus.iter().enumerate() {
        let chunk_id = store
            .insert_chunk(
                file_id,
                i as i64,
                &entry.content,
                Some(&entry.vector),
                "general",
            )
            .await?;
        usearch.add(chunk_id as u64, &entry.vector)?;
    }

    let searcher = HybridSearcher::new(
        store,
        Arc::new(RwLock::new(usearch)),
        HybridParams {
            top_k: sc.query.top_k.max(1),
            ..HybridParams::new()
        },
    );
    let hits = searcher
        .search(&sc.query.text, &sc.query.vector, None)
        .await?;

    if hits.len() < sc.expect.min_hits {
        anyhow::bail!(
            "rag_hybrid got {} hits, expected at least {}",
            hits.len(),
            sc.expect.min_hits
        );
    }
    let top = hits
        .first()
        .ok_or_else(|| anyhow::anyhow!("no hits returned"))?;
    if !top.content.contains(&sc.expect.top_contains) {
        anyhow::bail!(
            "top hit {:?} does not contain {:?}",
            top.content,
            sc.expect.top_contains
        );
    }
    Ok(())
}
