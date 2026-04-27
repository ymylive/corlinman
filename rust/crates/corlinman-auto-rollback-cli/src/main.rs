//! `corlinman-auto-rollback` CLI — Phase 3 Wave 1-B Step 4.
//!
//! Thin wrapper that loads a corlinman config, opens both
//! `evolution.sqlite` + `kb.sqlite`, builds an
//! [`corlinman_auto_rollback::AutoRollbackMonitor`] over a real
//! `EvolutionApplier`, and runs one [`AutoRollbackMonitor::run_once`]
//! pass. Designed to be invoked by `corlinman-scheduler` as a
//! subprocess job — same shape as W1-A's `corlinman-shadow-tester`.
//!
//! Lives in its own crate (`corlinman-auto-rollback-cli`) because
//! `corlinman-gateway` already depends on `corlinman-auto-rollback`'s
//! library; reversing that dep inside the latter — even on the bin
//! target only — produces a cargo cycle. Splitting the binary out
//! keeps the cycle gone without exposing gateway types from the
//! auto-rollback library.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use clap::{Parser, Subcommand};
use corlinman_auto_rollback::{Applier, AutoRollbackMonitor};
use corlinman_core::config::Config;
use corlinman_evolution::{EvolutionStore, HistoryRepo, ProposalsRepo};
use corlinman_gateway::evolution_applier::EvolutionApplier;
use corlinman_vector::SqliteStore;
use tracing::{error, info};
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

#[derive(Debug, Parser)]
#[command(
    name = "corlinman-auto-rollback",
    version,
    about = "AutoRollback — watches recently-applied EvolutionProposals for metrics regression and auto-reverts via the EvolutionApplier."
)]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Run one auto-rollback pass: list applied proposals in the grace
    /// window, compute metric deltas, revert anything whose delta
    /// breaches threshold. Designed for cron invocation via
    /// `corlinman-scheduler`.
    RunOnce(RunOnceArgs),
}

#[derive(Debug, Parser)]
struct RunOnceArgs {
    /// Path to the corlinman config (`corlinman.toml`). Reads
    /// `[evolution.auto_rollback]` + `[evolution.observer].db_path` +
    /// `[server].data_dir`.
    #[arg(long)]
    config: PathBuf,

    /// Per-run cap on proposals inspected; overrides the monitor's
    /// default (50) when set. Useful for one-off backfills.
    #[arg(long)]
    max_proposals: Option<usize>,
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    init_tracing();
    let cli = Cli::parse();
    match cli.command {
        Cmd::RunOnce(args) => run_once(args).await,
    }
}

fn init_tracing() {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt::layer().with_target(true))
        .init();
}

/// `run-once` flow:
///
/// 1. Load `Config` from `--config`.
/// 2. Bail if `[evolution.auto_rollback].enabled = false` — the
///    operator must explicitly opt in. Silent no-op would hide a
///    misconfigured cron.
/// 3. Open `EvolutionStore` at `[evolution.observer].db_path`, build
///    `ProposalsRepo` + `HistoryRepo`.
/// 4. Open the kb at `<data_dir>/kb.sqlite` (with `$CORLINMAN_DATA_DIR`
///    overriding `[server].data_dir`, mirroring shadow-tester +
///    gateway).
/// 5. Construct `EvolutionApplier::new(...)` against the same stores
///    and wrap as `Arc<dyn Applier>`.
/// 6. Build `AutoRollbackMonitor`, apply `--max-proposals` if set, run.
/// 7. One-line tracing summary + exit 0.
async fn run_once(args: RunOnceArgs) -> anyhow::Result<()> {
    let config = Config::load_from_path(&args.config).map_err(|e| {
        error!(path = %args.config.display(), error = %e, "auto_rollback: failed to load config");
        anyhow::anyhow!("load config {}: {e}", args.config.display())
    })?;

    let ar_cfg = &config.evolution.auto_rollback;
    if !ar_cfg.enabled {
        error!(
            "auto_rollback: [evolution.auto_rollback].enabled = false — refusing to run. \
             Set it to true once metrics_baseline rows have populated, or remove the cron job."
        );
        anyhow::bail!("auto_rollback disabled");
    }

    let evolution_db = config.evolution.observer.db_path.clone();
    let kb_path = resolve_kb_path(&config.server.data_dir);

    info!(
        evolution_db = %evolution_db.display(),
        kb_path = %kb_path.display(),
        grace_window_hours = ar_cfg.grace_window_hours,
        "auto_rollback: opening stores"
    );

    let evol = Arc::new(EvolutionStore::open(&evolution_db).await.map_err(|e| {
        error!(path = %evolution_db.display(), error = %e, "auto_rollback: open evolution.sqlite failed");
        anyhow::anyhow!("open {}: {e}", evolution_db.display())
    })?);
    let kb = Arc::new(SqliteStore::open(&kb_path).await.map_err(|e| {
        error!(path = %kb_path.display(), error = %e, "auto_rollback: open kb.sqlite failed");
        anyhow::anyhow!("open {}: {e}", kb_path.display())
    })?);

    let proposals = ProposalsRepo::new(evol.pool().clone());
    let history = HistoryRepo::new(evol.pool().clone());

    // EvolutionApplier owns the kb-mutation + audit-flip path; the
    // adapter `impl Applier for EvolutionApplier` (in gateway) maps
    // `ApplyError` → `RevertError`.
    let applier = EvolutionApplier::new(evol.clone(), kb.clone(), ar_cfg.thresholds.clone());
    let applier: Arc<dyn Applier> = Arc::new(applier);

    let mut monitor = AutoRollbackMonitor::new(
        proposals,
        history,
        evol.pool().clone(),
        applier,
        ar_cfg.clone(),
    );
    if let Some(n) = args.max_proposals {
        monitor = monitor.with_max_proposals_per_run(n);
    }

    let summary = monitor.run_once().await;

    info!(
        proposals_inspected = summary.proposals_inspected,
        thresholds_breached = summary.thresholds_breached,
        rollbacks_triggered = summary.rollbacks_triggered,
        rollbacks_succeeded = summary.rollbacks_succeeded,
        rollbacks_failed = summary.rollbacks_failed,
        errors = summary.errors,
        "auto_rollback: run-once complete"
    );

    Ok(())
}

/// Resolve kb.sqlite the same way the gateway + shadow-tester do: env
/// override (`CORLINMAN_DATA_DIR`) wins so dev / test invocations don't
/// have to rewrite the config; otherwise fall back to
/// `[server].data_dir`.
fn resolve_kb_path(config_data_dir: &Path) -> PathBuf {
    if let Ok(env_dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(env_dir).join("kb.sqlite");
    }
    config_data_dir.join("kb.sqlite")
}
