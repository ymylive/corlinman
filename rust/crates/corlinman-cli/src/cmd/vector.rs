//! `corlinman vector {stats,query,rebuild}` — operator surface for the
//! corlinman-vector hybrid index.
//!
//! - `stats`: counts + on-disk size, text or JSON.
//! - `query`: one-shot hybrid search; supports required/excluded tag
//!   filters pushed down through
//!   [`corlinman_vector::HybridSearcher`].
//! - `rebuild`: walk a knowledge directory, re-chunk every `*.md`, and
//!   atomically replace the `.usearch` file (rename from `.usearch.new`).
//!
//! Embedding is not wired yet: `rebuild` and `query` both fall back to a
//! deterministic hash-based stub vector and print a one-line notice so
//! the operator isn't surprised by the content.

use std::fs;
use std::path::PathBuf;

use anyhow::{anyhow, Context, Result};
use clap::Subcommand;
use corlinman_core::config::Config;
use corlinman_vector::{
    HybridParams, HybridSearcher, SqliteStore, TagFilter, UsearchIndex, VectorStore,
};
use serde::Serialize;

/// Deterministic embedding dimension for the hash-based stub vector.
/// Kept small so a brand-new index file stays tiny in tests.
const STUB_DIM: usize = 64;

#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// Show index stats: chunk count, file count, tag count, index size.
    Stats {
        /// Emit JSON instead of the human-readable summary.
        #[arg(long)]
        json: bool,
        /// Explicit config path; defaults to `$CORLINMAN_DATA_DIR/config.toml`.
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Run a test query against the vector store.
    Query {
        /// Query text fed to BM25 and (after embedding) to HNSW.
        query: String,
        /// Max hits to return.
        #[arg(short = 'k', long, default_value = "10")]
        top_k: usize,
        /// Tags that every returned chunk must carry. Repeat for AND.
        #[arg(long = "tag")]
        tag: Vec<String>,
        /// Tags that returned chunks must NOT carry.
        #[arg(long)]
        exclude: Vec<String>,
        /// Emit JSON instead of the text summary.
        #[arg(long)]
        json: bool,
        /// Explicit config path; defaults to `$CORLINMAN_DATA_DIR/config.toml`.
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Rebuild the index from a knowledge-source directory.
    Rebuild {
        /// Directory to scan; defaults to `$data_dir/knowledge`.
        #[arg(long)]
        source: Option<PathBuf>,
        /// Required to actually write. Dry-run prints a plan otherwise.
        #[arg(long)]
        confirm: bool,
        /// Explicit config path; defaults to `$CORLINMAN_DATA_DIR/config.toml`.
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// List the distinct `chunks.namespace` values with their chunk counts.
    ///
    /// Sprint 9 T1: namespaces partition the corpus into general / diary:<agent> /
    /// papers / etc. Legacy chunks default to `general`.
    Namespaces {
        /// Emit JSON instead of the text summary.
        #[arg(long)]
        json: bool,
        /// Explicit config path; defaults to `$CORLINMAN_DATA_DIR/config.toml`.
        #[arg(long)]
        path: Option<PathBuf>,
    },
}

pub async fn run(cmd: Cmd) -> Result<()> {
    match cmd {
        Cmd::Stats { json, path } => stats(path, json).await,
        Cmd::Query {
            query,
            top_k,
            tag,
            exclude,
            json,
            path,
        } => query_cmd(path, query, top_k, tag, exclude, json).await,
        Cmd::Rebuild {
            source,
            confirm,
            path,
        } => rebuild(path, source, confirm).await,
        Cmd::Namespaces { json, path } => namespaces_cmd(path, json).await,
    }
}

// ---------------------------------------------------------------------------
// stats
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct StatsOutput {
    chunks: i64,
    files: i64,
    tags: i64,
    index_bytes: u64,
    updated_at: Option<String>,
}

async fn stats(config_path: Option<PathBuf>, json: bool) -> Result<()> {
    let cfg = load_config(config_path)?;
    let (sqlite_path, usearch_path) = index_paths(&cfg);

    // Ensure the parent dir exists so `SqliteStore::open` can
    // `create_if_missing(true)` on a fresh install.
    if let Some(parent) = sqlite_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }

    // Missing DB ⇒ treat as empty rather than erroring out. Operators
    // running `stats` on a fresh checkout shouldn't be punished.
    let store = SqliteStore::open(&sqlite_path)
        .await
        .with_context(|| format!("open sqlite '{}'", sqlite_path.display()))?;
    let chunks = store.count_chunks().await?;
    let files = store.count_files().await?;
    let tags = store.count_tags().await?;
    let index_bytes = match fs::metadata(&usearch_path) {
        Ok(m) => m.len(),
        Err(_) => 0,
    };
    let updated_at = fs::metadata(&sqlite_path)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| {
            time::OffsetDateTime::from(t)
                .format(&time::format_description::well_known::Rfc3339)
                .ok()
        });

    let out = StatsOutput {
        chunks,
        files,
        tags,
        index_bytes,
        updated_at,
    };

    if json {
        println!("{}", serde_json::to_string(&out)?);
    } else {
        println!("Chunks:  {}", out.chunks);
        println!("Files:   {}", out.files);
        println!("Tags:    {}", out.tags);
        println!("Index:   {}", human_bytes(out.index_bytes));
        if let Some(ts) = &out.updated_at {
            println!("Updated: {ts}");
        } else {
            println!("Updated: (unknown)");
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// query
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct HitOutput {
    chunk_id: i64,
    score: f32,
    path: String,
    content: String,
}

async fn query_cmd(
    config_path: Option<PathBuf>,
    query: String,
    top_k: usize,
    tag: Vec<String>,
    exclude: Vec<String>,
    json: bool,
) -> Result<()> {
    let cfg = load_config(config_path)?;
    let (sqlite_path, usearch_path) = index_paths(&cfg);
    if !sqlite_path.exists() {
        return Err(anyhow!(
            "no index at {} — run `corlinman vector rebuild` first",
            sqlite_path.display()
        ));
    }

    // No embedding provider is wired yet; use a deterministic stub so the
    // command is runnable end-to-end. BM25 is unaffected.
    eprintln!(
        "note: no embedding provider configured; using hash-based stub vectors \
         (BM25 path is unaffected)"
    );
    let query_vector = stub_embed(&query, STUB_DIM);

    let store = VectorStore::open(&sqlite_path, &usearch_path)
        .await
        .with_context(|| format!("open vector store at {}", sqlite_path.display()))?;

    // Hand-roll a searcher so we can set `tag_filter` via HybridParams.
    let params = HybridParams {
        top_k,
        tag_filter: build_tag_filter(&tag, &exclude),
        ..HybridParams::default()
    };
    let searcher = HybridSearcher::new(
        std::sync::Arc::new(
            SqliteStore::open(&sqlite_path)
                .await
                .with_context(|| format!("reopen sqlite '{}'", sqlite_path.display()))?,
        ),
        store.index().clone(),
        params.clone(),
    );
    let hits = searcher.search(&query, &query_vector, Some(params)).await?;

    let out: Vec<HitOutput> = hits
        .iter()
        .map(|h| HitOutput {
            chunk_id: h.chunk_id,
            score: h.score,
            path: h.path.clone(),
            content: h.content.clone(),
        })
        .collect();

    if json {
        println!("{}", serde_json::to_string(&out)?);
    } else if out.is_empty() {
        println!("(no hits)");
    } else {
        for (i, h) in out.iter().enumerate() {
            println!(
                "[{}] score={:.4}  chunk={}  path={}",
                i + 1,
                h.score,
                h.chunk_id,
                h.path
            );
            println!("    {}", truncate_for_display(&h.content, 200));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// namespaces
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct NamespaceOutput {
    namespace: String,
    chunks: u64,
}

async fn namespaces_cmd(config_path: Option<PathBuf>, json: bool) -> Result<()> {
    let cfg = load_config(config_path)?;
    let (sqlite_path, _usearch_path) = index_paths(&cfg);

    if let Some(parent) = sqlite_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }
    let store = SqliteStore::open(&sqlite_path)
        .await
        .with_context(|| format!("open sqlite '{}'", sqlite_path.display()))?;
    let rows = store.list_namespaces().await?;
    let out: Vec<NamespaceOutput> = rows
        .into_iter()
        .map(|(namespace, chunks)| NamespaceOutput { namespace, chunks })
        .collect();

    if json {
        println!("{}", serde_json::to_string(&out)?);
    } else if out.is_empty() {
        println!("(no namespaces — index is empty)");
    } else {
        for ns in &out {
            println!("{:<24} {:>8} chunks", ns.namespace, ns.chunks);
        }
    }
    Ok(())
}

fn build_tag_filter(required: &[String], excluded: &[String]) -> Option<TagFilter> {
    if required.is_empty() && excluded.is_empty() {
        return None;
    }
    Some(TagFilter {
        required: required.to_vec(),
        excluded: excluded.to_vec(),
        any_of: Vec::new(),
    })
}

// ---------------------------------------------------------------------------
// rebuild
// ---------------------------------------------------------------------------

async fn rebuild(
    config_path: Option<PathBuf>,
    source_override: Option<PathBuf>,
    confirm: bool,
) -> Result<()> {
    let cfg = load_config(config_path)?;
    let (sqlite_path, usearch_path) = index_paths(&cfg);
    let source = source_override.unwrap_or_else(|| cfg.server.data_dir.join("knowledge"));

    if !source.exists() {
        return Err(anyhow!(
            "source directory does not exist: {}",
            source.display()
        ));
    }

    // Plan: count files + chunks before touching anything on disk.
    let mut md_files: Vec<PathBuf> = Vec::new();
    walk_markdown(&source, &mut md_files)?;
    md_files.sort();

    let mut planned_chunks = 0_usize;
    for p in &md_files {
        let text = fs::read_to_string(p).with_context(|| format!("read {}", p.display()))?;
        planned_chunks += chunk_markdown(&text).len();
    }

    if !confirm {
        println!(
            "will rebuild {} file(s), {} chunk(s) from {}",
            md_files.len(),
            planned_chunks,
            source.display()
        );
        println!("re-run with --confirm to execute");
        return Ok(());
    }

    // Ensure the parent directory for the SQLite + usearch files exists.
    if let Some(parent) = sqlite_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }

    let store = SqliteStore::open(&sqlite_path)
        .await
        .with_context(|| format!("open sqlite '{}'", sqlite_path.display()))?;

    // Nuke any prior rows so the rebuild is a full replacement. ON
    // DELETE CASCADE takes chunk_tags + chunks with files.
    sqlx::query("DELETE FROM files")
        .execute(store.pool())
        .await
        .context("truncate files")?;

    let mut index = UsearchIndex::create(STUB_DIM).context("create usearch")?;

    let mut embedded = 0_usize;
    for path in &md_files {
        let text = fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
        let rel = path
            .strip_prefix(&source)
            .unwrap_or(path)
            .to_string_lossy()
            .into_owned();
        let size = text.len() as i64;
        let file_id = store.insert_file(&rel, "default", "stub", 0, size).await?;
        for (i, piece) in chunk_markdown(&text).into_iter().enumerate() {
            let v = stub_embed(&piece, STUB_DIM);
            let chunk_id = store
                .insert_chunk(file_id, i as i64, &piece, Some(&v), "general")
                .await?;
            index
                .add(chunk_id as u64, &v)
                .with_context(|| format!("hnsw add chunk={chunk_id}"))?;
            embedded += 1;
            if embedded % 50 == 0 {
                eprintln!("[{embedded}/{planned_chunks}] chunks embedded...");
            }
        }
    }

    // Atomic write: save to `.usearch.new`, rename over the live file.
    let tmp_path = usearch_path.with_extension("usearch.new");
    if let Some(parent) = tmp_path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }
    index
        .save(&tmp_path)
        .with_context(|| format!("save {}", tmp_path.display()))?;
    fs::rename(&tmp_path, &usearch_path)
        .with_context(|| format!("rename {} → {}", tmp_path.display(), usearch_path.display()))?;

    println!(
        "rebuilt: {} file(s), {} chunk(s), index at {}",
        md_files.len(),
        embedded,
        usearch_path.display()
    );
    Ok(())
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

fn load_config(explicit: Option<PathBuf>) -> Result<Config> {
    let path = explicit.unwrap_or_else(Config::default_path);
    if !path.exists() {
        // Fall back to defaults so `vector stats` works on a fresh
        // install that hasn't been `corlinman config init`-ed yet, but
        // still honour `CORLINMAN_DATA_DIR` for the data-dir lookup.
        let mut cfg = Config::default();
        if let Some(env_dir) = std::env::var_os("CORLINMAN_DATA_DIR") {
            cfg.server.data_dir = PathBuf::from(env_dir);
        }
        return Ok(cfg);
    }
    Config::load_from_path(&path).with_context(|| format!("load config from {}", path.display()))
}

fn index_paths(cfg: &Config) -> (PathBuf, PathBuf) {
    let dir = &cfg.server.data_dir;
    (
        dir.join("knowledge_base.sqlite"),
        dir.join("knowledge_base.usearch"),
    )
}

/// Cheap deterministic embedding: hash the input into a unit-norm
/// `STUB_DIM`-sized vector. Stable across runs so `query` returns
/// reproducible results when the real provider isn't configured.
fn stub_embed(text: &str, dim: usize) -> Vec<f32> {
    use sha2::{Digest, Sha256};
    let mut v = vec![0.0_f32; dim];
    let bytes = Sha256::digest(text.as_bytes());
    for (i, slot) in v.iter_mut().enumerate() {
        let b = bytes[i % bytes.len()] as f32;
        *slot = (b / 255.0) - 0.5;
    }
    // Normalise so cosine distance is well-behaved.
    let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > 0.0 {
        for x in &mut v {
            *x /= norm;
        }
    }
    v
}

/// Simple paragraph-based chunker: split on blank lines, trim, drop
/// empties. Good enough for the CLI smoke-test; the real indexer uses a
/// token-aware splitter (future work).
fn chunk_markdown(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut buf = String::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            if !buf.trim().is_empty() {
                out.push(buf.trim().to_string());
            }
            buf.clear();
        } else {
            buf.push_str(line);
            buf.push('\n');
        }
    }
    if !buf.trim().is_empty() {
        out.push(buf.trim().to_string());
    }
    out
}

fn walk_markdown(dir: &std::path::Path, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(dir).with_context(|| format!("read_dir {}", dir.display()))? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            walk_markdown(&path, out)?;
        } else if path.extension().and_then(|s| s.to_str()) == Some("md") {
            out.push(path);
        }
    }
    Ok(())
}

fn human_bytes(n: u64) -> String {
    const KB: f64 = 1024.0;
    const MB: f64 = KB * 1024.0;
    const GB: f64 = MB * 1024.0;
    let n = n as f64;
    if n >= GB {
        format!("{:.2} GB", n / GB)
    } else if n >= MB {
        format!("{:.2} MB", n / MB)
    } else if n >= KB {
        format!("{:.2} KB", n / KB)
    } else {
        format!("{n} B")
    }
}

fn truncate_for_display(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.replace('\n', " ");
    }
    let mut taken: String = s.chars().take(max).collect();
    taken.push('…');
    taken.replace('\n', " ")
}

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunk_markdown_splits_on_blank_lines() {
        let text = "para one\nline two\n\npara two\n\n\npara three\n";
        let chunks = chunk_markdown(text);
        assert_eq!(chunks.len(), 3);
        assert_eq!(chunks[0], "para one\nline two");
        assert_eq!(chunks[1], "para two");
        assert_eq!(chunks[2], "para three");
    }

    #[test]
    fn stub_embed_is_unit_norm_and_deterministic() {
        let a = stub_embed("hello world", STUB_DIM);
        let b = stub_embed("hello world", STUB_DIM);
        assert_eq!(a, b);
        let norm: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 1e-4, "norm={norm}");
    }

    #[test]
    fn build_tag_filter_short_circuits_when_both_empty() {
        assert!(build_tag_filter(&[], &[]).is_none());
        let tf = build_tag_filter(&["rust".into()], &[]).unwrap();
        assert_eq!(tf.required, vec!["rust".to_string()]);
        assert!(tf.excluded.is_empty());
    }

    #[test]
    fn human_bytes_picks_the_right_unit() {
        assert_eq!(human_bytes(0), "0 B");
        assert_eq!(human_bytes(1023), "1023 B");
        assert_eq!(human_bytes(2048), "2.00 KB");
        assert_eq!(human_bytes(1024 * 1024 * 5), "5.00 MB");
    }
}
