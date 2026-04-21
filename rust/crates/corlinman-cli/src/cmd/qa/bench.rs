//! `corlinman qa bench` — perf baseline for the 1.0 release (plan §10 T5).
//!
//! Measures wall-clock latency for three critical paths:
//!   1. `/v1/chat/completions` against an in-process gateway + scripted
//!      backend. Token-only response, non-streaming.
//!   2. RAG hybrid search over a small fixture corpus.
//!   3. Plugin stdio roundtrip — tiny python echo plugin.
//!
//! Prints an ASCII table; optionally appends the same table (Markdown)
//! to `--report <file>` so CI can diff against `docs/perf-baseline-1.0.md`.

use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use axum::{
    body::{to_bytes, Body},
    http::Request,
};
use corlinman_core::CorlinmanError;
use corlinman_gateway::routes::chat::{BackendRx, ChatBackend, ChatState};
use corlinman_gateway::routes::router_with_chat_state;
use corlinman_plugins::manifest::{parse_manifest_file, PluginManifest};
use corlinman_plugins::runtime::{jsonrpc_stdio, PluginOutput};
use corlinman_proto::v1::{server_frame, ChatStart, ClientFrame, Done, ServerFrame, TokenDelta};
use corlinman_vector::{HybridParams, HybridSearcher, SqliteStore, UsearchIndex};
use futures::{stream, Stream};
use tokio::sync::{mpsc, RwLock};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

use super::{percentile, BenchArgs};

pub async fn run_bench(args: BenchArgs) -> anyhow::Result<()> {
    let chat = bench_chat(&args).await?;
    let rag = bench_rag(&args).await?;
    let plugin = bench_plugin(&args).await?;

    let rows = vec![chat, rag, plugin];
    let report = render_markdown(&rows);
    println!("{report}");

    if let Some(path) = &args.report {
        std::fs::write(path, &report)
            .map_err(|e| anyhow::anyhow!("write report {}: {e}", path.display()))?;
        println!("wrote {}", path.display());
    }
    Ok(())
}

/// One workload's summary row.
pub struct BenchRow {
    pub name: &'static str,
    pub iterations: usize,
    pub p50: Duration,
    pub p99: Duration,
    pub mean: Duration,
}

async fn bench_chat(args: &BenchArgs) -> anyhow::Result<BenchRow> {
    let mut samples: Vec<Duration> = Vec::with_capacity(args.iterations);

    for i in 0..(args.warmup + args.iterations) {
        let frames = vec![
            ServerFrame {
                kind: Some(server_frame::Kind::Token(TokenDelta {
                    text: "hello".into(),
                    is_reasoning: false,
                    seq: 0,
                })),
            },
            ServerFrame {
                kind: Some(server_frame::Kind::Done(Done {
                    finish_reason: "stop".into(),
                    usage: None,
                    total_tokens_seen: 0,
                    wall_time_ms: 0,
                })),
            },
        ];
        let backend = Arc::new(BenchBackend::new(frames));
        let app = router_with_chat_state(ChatState::new(backend));

        let body = serde_json::json!({
            "model": "bench-model",
            "stream": false,
            "messages": [{"role": "user", "content": "hi"}],
        });
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&body)?))?;

        let t0 = Instant::now();
        let resp = app.oneshot(req).await?;
        let _ = to_bytes(resp.into_body(), 1 << 20).await?;
        let elapsed = t0.elapsed();
        if i >= args.warmup {
            samples.push(elapsed);
        }
    }

    Ok(summarise("chat_completions", samples))
}

async fn bench_rag(args: &BenchArgs) -> anyhow::Result<BenchRow> {
    // Build a single tempdir SQLite + usearch pair; reuse across iterations.
    let tmp = tempfile::tempdir()?;
    let path = tmp.path().join("bench.sqlite");
    let store = Arc::new(SqliteStore::open(&path).await?);
    let file_id = store
        .insert_file("bench://corpus.md", "default", "chk", 0, 0)
        .await?;

    let dim = 8;
    let mut idx = UsearchIndex::create_with_capacity(dim, 256)?;

    // 128 chunks is plenty to exercise the hybrid path without ballooning
    // test time; each content is deterministic so BM25 can match.
    let corpus: Vec<(String, Vec<f32>)> = (0..128)
        .map(|i| {
            let text = format!("bench chunk {i} about topic alpha and beta");
            let mut v = vec![0.0_f32; dim];
            v[i % dim] = 1.0 + (i as f32) * 0.01;
            (text, v)
        })
        .collect();
    for (i, (text, v)) in corpus.iter().enumerate() {
        let chunk_id = store
            .insert_chunk(file_id, i as i64, text, Some(v), "general")
            .await?;
        idx.add(chunk_id as u64, v)?;
    }

    let searcher = HybridSearcher::new(
        store,
        Arc::new(RwLock::new(idx)),
        HybridParams {
            top_k: 5,
            ..HybridParams::new()
        },
    );

    let query_text = "alpha beta topic";
    let query_vec = {
        let mut v = vec![0.0_f32; dim];
        v[0] = 1.0;
        v
    };

    let mut samples: Vec<Duration> = Vec::with_capacity(args.iterations);
    for i in 0..(args.warmup + args.iterations) {
        let t0 = Instant::now();
        let _ = searcher.search(query_text, &query_vec, None).await?;
        let elapsed = t0.elapsed();
        if i >= args.warmup {
            samples.push(elapsed);
        }
    }
    Ok(summarise("rag_hybrid", samples))
}

async fn bench_plugin(args: &BenchArgs) -> anyhow::Result<BenchRow> {
    let Some(py) = which_python() else {
        anyhow::bail!("python3/python not on PATH; cannot bench plugin");
    };
    let tmp = tempfile::tempdir()?;
    let plugin_dir = tmp.path().join("echo");
    std::fs::create_dir_all(&plugin_dir)?;
    std::fs::write(plugin_dir.join("main.py"), ECHO_SCRIPT)?;
    let manifest_body = format!(
        r#"name = "echo"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "{py}"
args = ["main.py"]
[[capabilities.tools]]
name = "echo"
"#
    );
    let manifest_path = plugin_dir.join("plugin-manifest.toml");
    std::fs::write(&manifest_path, manifest_body)?;
    let manifest: PluginManifest = parse_manifest_file(&manifest_path)?;

    // Smaller iteration count — python spawn is O(10ms).
    let iters = args.iterations.min(80);
    let warmup = args.warmup.min(4);
    let args_json = b"{\"mode\":\"echo\",\"greeting\":\"hi\"}";

    let mut samples: Vec<Duration> = Vec::with_capacity(iters);
    for i in 0..(warmup + iters) {
        let t0 = Instant::now();
        let out = jsonrpc_stdio::execute(
            &manifest.name,
            "echo",
            &plugin_dir,
            Some(&manifest),
            None,
            args_json,
            "bench-session",
            &format!("bench-req-{i}"),
            "bench-trace",
            None,
            &[],
            CancellationToken::new(),
        )
        .await?;
        let _ = matches!(out, PluginOutput::Success { .. });
        let elapsed = t0.elapsed();
        if i >= warmup {
            samples.push(elapsed);
        }
    }
    Ok(summarise("plugin_stdio", samples))
}

fn summarise(name: &'static str, mut samples: Vec<Duration>) -> BenchRow {
    let iterations = samples.len();
    let mean = if samples.is_empty() {
        Duration::ZERO
    } else {
        let total: Duration = samples.iter().copied().sum();
        total / samples.len() as u32
    };
    let p50 = percentile(&mut samples, 0.50);
    let p99 = percentile(&mut samples, 0.99);
    BenchRow {
        name,
        iterations,
        p50,
        p99,
        mean,
    }
}

fn render_markdown(rows: &[BenchRow]) -> String {
    let mut s = String::new();
    s.push_str("| workload | iterations | p50 | p99 | mean |\n");
    s.push_str("|---|---:|---:|---:|---:|\n");
    for r in rows {
        s.push_str(&format!(
            "| {} | {} | {} | {} | {} |\n",
            r.name,
            r.iterations,
            fmt_dur(r.p50),
            fmt_dur(r.p99),
            fmt_dur(r.mean),
        ));
    }
    s
}

fn fmt_dur(d: Duration) -> String {
    let ms = d.as_secs_f64() * 1000.0;
    format!("{ms:.2}ms")
}

// ---- plumbing ---------------------------------------------------------------

#[derive(Clone)]
struct BenchBackend {
    frames: Arc<tokio::sync::Mutex<Vec<ServerFrame>>>,
}

impl BenchBackend {
    fn new(frames: Vec<ServerFrame>) -> Self {
        Self {
            frames: Arc::new(tokio::sync::Mutex::new(frames)),
        }
    }
}

#[async_trait]
impl ChatBackend for BenchBackend {
    async fn start(
        &self,
        _start: ChatStart,
    ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
        let (tx, _rx) = mpsc::channel::<ClientFrame>(16);
        let frames: Vec<_> = std::mem::take(&mut *self.frames.lock().await)
            .into_iter()
            .map(Ok)
            .collect();
        let s: BackendRx = Box::pin(stream::iter(frames))
            as Pin<Box<dyn Stream<Item = Result<ServerFrame, CorlinmanError>> + Send>>;
        Ok((tx, s))
    }
}

const ECHO_SCRIPT: &str = r#"
import json, sys
line = sys.stdin.readline()
req = json.loads(line) if line else {}
resp = {"jsonrpc": "2.0", "id": req.get("id", 1), "result": {"echo": (req.get("params") or {}).get("arguments", {})}}
sys.stdout.write(json.dumps(resp))
sys.stdout.write("\n")
sys.stdout.flush()
"#;

fn which_python() -> Option<String> {
    for candidate in ["python3", "python"] {
        if which_bin(candidate).is_some() {
            return Some(candidate.to_string());
        }
    }
    None
}

fn which_bin(bin: &str) -> Option<std::path::PathBuf> {
    let path_env = std::env::var("PATH").ok()?;
    for dir in path_env.split(':') {
        let candidate = std::path::Path::new(dir).join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}
