//! `corlinman qa {run,bench}` — YAML scenario runner + perf bench (plan §10 T2/T5).
//!
//! Each YAML under `qa/scenarios/*.yaml` picks a `kind`:
//!   * `chat_http` — in-process gateway with a scripted `ChatBackend`
//!     that replays the frames declared in the YAML
//!     (`frames: [{kind: token, text: "…"}, {kind: done}]`) and checks a
//!     JSON-shaped or SSE-shaped assertion block.
//!   * `plugin_exec_sync` — spawns a tiny `python3` echo plugin via
//!     `corlinman_plugins::runtime::jsonrpc_stdio::execute` and asserts
//!     the returned payload.
//!   * `plugin_exec_async` — same runtime, but the plugin emits
//!     `{task_id}` → expects `PluginOutput::AcceptedForLater`.
//!   * `rag_hybrid` — builds an in-memory `SqliteStore` + usearch index
//!     from the YAML corpus, runs `HybridSearcher::search`, asserts top
//!     hits.
//!   * `live` — placeholder that always skips unless the runner was
//!     invoked with `--include-live`.
//!
//! `corlinman qa bench` is a sibling that drives the same in-process stack
//! with N repeated calls, sorts the latency samples, and prints
//! p50/p99/count. Its output is what `docs/perf-baseline-1.0.md` records.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use clap::{Parser, Subcommand};

pub mod bench;
pub mod runner;
pub mod scenario;

use runner::{run_scenario, ScenarioOutcome};

/// Back-compat wrapper: `main.rs` still calls `cmd::qa::run(Args)`.
#[derive(Debug, Parser)]
pub struct Args {
    #[command(subcommand)]
    pub cmd: Cmd,
}

/// `corlinman qa …` subcommands.
#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// Load every `*.yaml` under `--scenarios-dir` and execute each scenario
    /// in declaration order. Exits non-zero iff any scenario failed.
    Run(RunArgs),
    /// Benchmark the critical paths (chat / rag / plugin) and print
    /// p50/p99 latencies. Results are stable enough to copy into
    /// `docs/perf-baseline-1.0.md`.
    Bench(BenchArgs),
}

#[derive(Debug, Parser)]
pub struct RunArgs {
    /// Directory containing scenario YAMLs. Defaults to `qa/scenarios`.
    #[arg(long, default_value = "qa/scenarios")]
    pub scenarios_dir: PathBuf,
    /// Optional substring filter on scenario file stems.
    #[arg(long)]
    pub filter: Option<String>,
    /// Execute scenarios with `requires_live: true`. Without this flag they
    /// report `skipped` (exit 0) — the default so CI stays offline.
    #[arg(long)]
    pub include_live: bool,
}

#[derive(Debug, Parser)]
pub struct BenchArgs {
    /// How many iterations per workload.
    #[arg(long, default_value_t = 200)]
    pub iterations: usize,
    /// How many warm-up iterations to run (and discard) before measuring.
    #[arg(long, default_value_t = 20)]
    pub warmup: usize,
    /// Optional path — when provided, a Markdown table is appended to the file.
    #[arg(long)]
    pub report: Option<PathBuf>,
}

pub async fn run(args: Args) -> anyhow::Result<()> {
    match args.cmd {
        Cmd::Run(run_args) => run_suite(run_args).await,
        Cmd::Bench(bench_args) => bench::run_bench(bench_args).await,
    }
}

async fn run_suite(args: RunArgs) -> anyhow::Result<()> {
    let scenarios = scenario::load_dir(&args.scenarios_dir, args.filter.as_deref())?;
    if scenarios.is_empty() {
        println!(
            "no scenarios found under {} (filter={:?})",
            args.scenarios_dir.display(),
            args.filter
        );
        return Ok(());
    }

    let mut summary: BTreeMap<&'static str, usize> = BTreeMap::new();
    let mut rows: Vec<(String, ScenarioOutcome, Duration)> = Vec::new();
    let started = Instant::now();

    for sc in &scenarios {
        let t0 = Instant::now();
        let outcome = run_scenario(sc, args.include_live).await;
        let elapsed = t0.elapsed();
        let tag = outcome.tag();
        *summary.entry(tag).or_insert(0) += 1;
        rows.push((sc.name.clone(), outcome, elapsed));
    }

    print_report(&rows, started.elapsed());

    let failed = summary.get("FAIL").copied().unwrap_or(0);
    if failed > 0 {
        std::process::exit(1);
    }
    Ok(())
}

fn print_report(rows: &[(String, ScenarioOutcome, Duration)], total: Duration) {
    let name_w = rows
        .iter()
        .map(|(n, _, _)| n.len())
        .max()
        .unwrap_or(0)
        .max(12);
    let mut passed = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    for (name, outcome, elapsed) in rows {
        let glyph = match outcome {
            ScenarioOutcome::Pass => {
                passed += 1;
                "PASS"
            }
            ScenarioOutcome::Skip { .. } => {
                skipped += 1;
                "SKIP"
            }
            ScenarioOutcome::Fail { .. } => {
                failed += 1;
                "FAIL"
            }
        };
        let detail = match outcome {
            ScenarioOutcome::Pass => String::new(),
            ScenarioOutcome::Skip { reason } => format!(" — {reason}"),
            ScenarioOutcome::Fail { reason } => format!(" — {reason}"),
        };
        println!(
            "{glyph:>4}  {name:<width$}  {elapsed:>7.2?}{detail}",
            width = name_w
        );
    }
    println!();
    println!(
        "{} passed, {} skipped, {} failed in {:.2?}",
        passed, skipped, failed, total
    );
}

/// Helper used by [`bench`] and scenario runners to sort+percentile a
/// latency sample vector.
pub(crate) fn percentile(samples: &mut [Duration], p: f64) -> Duration {
    if samples.is_empty() {
        return Duration::ZERO;
    }
    samples.sort_unstable();
    let idx = ((samples.len() as f64) * p).ceil() as usize;
    let idx = idx.saturating_sub(1).min(samples.len() - 1);
    samples[idx]
}
