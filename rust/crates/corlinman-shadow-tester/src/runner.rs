//! `ShadowRunner` — pulls pending medium/high-risk proposals, dispatches
//! to the per-kind simulator, writes results back.
//!
//! Per `run_once` invocation the runner:
//!
//! 1. For each registered [`KindSimulator`], asks the proposals repo for
//!    Pending rows of that kind whose risk is in `shadow_risks`.
//! 2. Atomically claims each row (`Pending → ShadowRunning`); a losing
//!    racer skips silently — exactly-one-runner is enforced at the DB.
//! 3. Loads the eval set for the kind, replays per-case `kb_seed` SQL
//!    against a tempdir copy of `kb.sqlite`, hands the kb path to the
//!    simulator, captures its [`SimulatorOutput`].
//! 4. Aggregates per-case `baseline` / `shadow` maps into one
//!    proposal-level baseline + shadow JSON blob and writes everything
//!    back via `mark_shadow_done` (`ShadowRunning → ShadowDone`).
//!
//! Failure isolation: a panicking or erroring simulator does not poison
//! the run — the case is recorded with `passed=false` + `error=...` and
//! the runner moves on. A simulator-less kind is silently skipped (no
//! claim) so an operator can register simulators incrementally.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use corlinman_evolution::{EvolutionKind, EvolutionRisk, ProposalId, ProposalsRepo};
use serde_json::{json, Value};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions};
use std::str::FromStr;
use tempfile::TempDir;
use tracing::{info, warn};

use crate::eval::{load_eval_set, EvalCase, EvalLoadError};
use crate::simulator::{KindSimulator, SimulatorOutput};

/// Counts surfaced by `run_once` so the caller (gateway/scheduler) can
/// log a one-line summary or expose Prometheus counters.
#[derive(Debug, Default, Clone)]
pub struct RunSummary {
    pub proposals_claimed: usize,
    pub proposals_completed: usize,
    pub proposals_failed: usize,
    pub cases_run: usize,
    pub errors: usize,
}

pub struct ShadowRunner {
    proposals: ProposalsRepo,
    /// Production kb.sqlite. Copied to a tempdir per case; never opened
    /// by the runner directly. Missing path triggers a logged warn +
    /// inline empty-kb bootstrap so tests / fresh installs work.
    kb_path: PathBuf,
    eval_set_dir: PathBuf,
    simulators: HashMap<EvolutionKind, Arc<dyn KindSimulator>>,
    /// Risks ShadowTester gates on. Low-risk proposals skip shadow
    /// entirely and remain on the original `pending → approved` path.
    shadow_risks: Vec<EvolutionRisk>,
    max_proposals_per_run: usize,
}

impl ShadowRunner {
    pub fn new(proposals: ProposalsRepo, kb_path: PathBuf, eval_set_dir: PathBuf) -> Self {
        Self {
            proposals,
            kb_path,
            eval_set_dir,
            simulators: HashMap::new(),
            shadow_risks: vec![EvolutionRisk::Medium, EvolutionRisk::High],
            max_proposals_per_run: 10,
        }
    }

    /// Tunable knobs — primarily for tests and operator overrides.
    pub fn with_shadow_risks(mut self, risks: Vec<EvolutionRisk>) -> Self {
        self.shadow_risks = risks;
        self
    }

    pub fn with_max_proposals_per_run(mut self, n: usize) -> Self {
        self.max_proposals_per_run = n;
        self
    }

    pub fn register_simulator(&mut self, sim: Arc<dyn KindSimulator>) {
        self.simulators.insert(sim.kind(), sim);
    }

    pub async fn run_once(&self) -> RunSummary {
        let mut summary = RunSummary::default();

        // Per-kind so unrelated simulators don't share an eval set.
        for (kind, simulator) in &self.simulators {
            let kind = *kind;
            let pending = match self
                .proposals
                .list_pending_for_shadow(
                    kind,
                    &self.shadow_risks,
                    self.max_proposals_per_run as i64,
                )
                .await
            {
                Ok(p) => p,
                Err(e) => {
                    warn!(error = %e, ?kind, "shadow: list_pending_for_shadow failed");
                    summary.errors += 1;
                    continue;
                }
            };

            for proposal in pending {
                // claim races: losers see NotFound and skip without
                // touching the row.
                if let Err(e) = self.proposals.claim_for_shadow(&proposal.id).await {
                    info!(
                        proposal_id = %proposal.id,
                        error = %e,
                        "shadow: claim_for_shadow lost race or row missing — skipping"
                    );
                    continue;
                }
                summary.proposals_claimed += 1;

                match self.run_proposal(kind, simulator.as_ref(), &proposal.id).await {
                    Ok(cases) => {
                        summary.cases_run += cases;
                        summary.proposals_completed += 1;
                    }
                    Err(e) => {
                        warn!(
                            proposal_id = %proposal.id,
                            error = %e,
                            "shadow: proposal failed during shadow run"
                        );
                        summary.proposals_failed += 1;
                        summary.errors += 1;
                    }
                }
            }
        }

        summary
    }

    /// Run one claimed proposal end-to-end. Returns the number of cases
    /// executed. Any error here means the row stays in `ShadowRunning`
    /// — the caller should surface that and (eventually) reap stuck
    /// rows.
    async fn run_proposal(
        &self,
        kind: EvolutionKind,
        simulator: &dyn KindSimulator,
        proposal_id: &ProposalId,
    ) -> Result<usize, RunError> {
        let eval_run_id = make_eval_run_id();

        // Empty / missing eval set: no_eval_set marker so the operator
        // sees a finished proposal with a clear "untested" label rather
        // than a stuck shadow_running row.
        let set = match load_eval_set(&self.eval_set_dir, kind).await {
            Ok(s) => s,
            Err(EvalLoadError::EmptySet { .. }) | Err(EvalLoadError::MissingDir(_)) => {
                warn!(
                    proposal_id = %proposal_id,
                    ?kind,
                    eval_set_dir = %self.eval_set_dir.display(),
                    "shadow: no eval set for kind — recording no_eval_set"
                );
                let empty = json!({});
                let shadow = json!({
                    "eval_run_id": "no-eval-set",
                    "kind": kind.as_str(),
                    "total_cases": 0,
                    "passed_cases": 0,
                    "failed_cases": [],
                    "pass_rate": 0.0,
                    "p50_latency_ms": 0,
                    "p95_latency_ms": 0,
                    "per_case_shadow": [],
                });
                self.proposals
                    .mark_shadow_done(proposal_id, "no-eval-set", &empty, &shadow)
                    .await
                    .map_err(RunError::Repo)?;
                return Ok(0);
            }
            Err(e) => return Err(RunError::EvalLoad(e)),
        };

        let mut outputs: Vec<SimulatorOutput> = Vec::with_capacity(set.cases.len());
        for case in &set.cases {
            let output = self.run_case(simulator, case).await;
            outputs.push(output);
        }
        let cases_run = outputs.len();

        let (baseline_agg, shadow_agg) = aggregate(&eval_run_id, kind, &outputs);
        self.proposals
            .mark_shadow_done(proposal_id, &eval_run_id, &baseline_agg, &shadow_agg)
            .await
            .map_err(RunError::Repo)?;
        Ok(cases_run)
    }

    /// Run one case in its own tempdir. Errors are downgraded to a
    /// failed `SimulatorOutput` so one bad case doesn't tank the set.
    async fn run_case(
        &self,
        simulator: &dyn KindSimulator,
        case: &EvalCase,
    ) -> SimulatorOutput {
        let tmp = match TempDir::new() {
            Ok(t) => t,
            Err(e) => return failed_output(case, format!("tempdir: {e}")),
        };
        let kb_path = tmp.path().join("kb.sqlite");

        if let Err(e) = self.materialize_kb(&kb_path).await {
            return failed_output(case, format!("kb materialize: {e}"));
        }
        if let Err(e) = replay_seed(&kb_path, &case.kb_seed).await {
            return failed_output(case, format!("kb seed: {e}"));
        }

        match simulator.simulate(case, &kb_path).await {
            Ok(out) => out,
            Err(e) => failed_output(case, e.to_string()),
        }
        // tmp drops here; sandbox vanishes.
    }

    /// Either copy the production kb (normal path) or bootstrap an
    /// empty schema inline (fallback when prod kb is absent — typical
    /// for tests and fresh installs).
    async fn materialize_kb(&self, dest: &Path) -> Result<(), String> {
        if tokio::fs::try_exists(&self.kb_path)
            .await
            .map_err(|e| e.to_string())?
        {
            tokio::fs::copy(&self.kb_path, dest)
                .await
                .map(|_| ())
                .map_err(|e| e.to_string())
        } else {
            warn!(
                kb_path = %self.kb_path.display(),
                "shadow: production kb missing — bootstrapping empty kb"
            );
            bootstrap_empty_kb(dest).await
        }
    }
}

/// Internal-only error to keep `run_proposal` ergonomic. Surfaced as a
/// log line; the proposal stays `ShadowRunning` for an out-of-band reaper
/// to handle.
#[derive(Debug, thiserror::Error)]
enum RunError {
    #[error("eval load: {0}")]
    EvalLoad(EvalLoadError),
    #[error("repo: {0}")]
    Repo(corlinman_evolution::RepoError),
}

fn failed_output(case: &EvalCase, error: String) -> SimulatorOutput {
    SimulatorOutput {
        case_name: case.name.clone(),
        passed: false,
        baseline: serde_json::Map::new(),
        shadow: serde_json::Map::new(),
        latency_ms: 0,
        error: Some(error),
    }
}

/// Replay `kb_seed` SQL against a one-shot pool. We close the pool
/// before the simulator opens its own — SQLite + WAL is fine with
/// concurrent readers but tests run faster with one-at-a-time setup.
async fn replay_seed(kb_path: &Path, seed: &[String]) -> Result<(), String> {
    if seed.is_empty() {
        return Ok(());
    }
    let url = format!("sqlite://{}", kb_path.display());
    let opts = SqliteConnectOptions::from_str(&url)
        .map_err(|e| e.to_string())?
        .create_if_missing(true);
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(opts)
        .await
        .map_err(|e| e.to_string())?;
    for stmt in seed {
        sqlx::raw_sql(stmt)
            .execute(&pool)
            .await
            .map_err(|e| format!("seed stmt {stmt:?}: {e}"))?;
    }
    pool.close().await;
    Ok(())
}

/// Minimal kb schema for the fallback path. Mirrors the columns
/// memory_op fixtures rely on. We don't pull `corlinman-vector` here —
/// that crate ships the prod schema, but the shadow runner only needs
/// enough surface for the in-process simulator.
async fn bootstrap_empty_kb(dest: &Path) -> Result<(), String> {
    let url = format!("sqlite://{}", dest.display());
    let opts = SqliteConnectOptions::from_str(&url)
        .map_err(|e| e.to_string())?
        .create_if_missing(true);
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(opts)
        .await
        .map_err(|e| e.to_string())?;
    sqlx::raw_sql(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, diary_name TEXT, checksum TEXT, mtime INTEGER, size INTEGER);
         CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER, chunk_index INTEGER, content TEXT, namespace TEXT DEFAULT 'general');",
    )
    .execute(&pool)
    .await
    .map_err(|e| e.to_string())?;
    pool.close().await;
    Ok(())
}

/// `eval-YYYY-MM-DD-<short-uuid>` — date for human grepping, uuid for
/// uniqueness across runners on the same day.
fn make_eval_run_id() -> String {
    let now = time::OffsetDateTime::now_utc();
    let date = format!(
        "{:04}-{:02}-{:02}",
        now.year(),
        u8::from(now.month()),
        now.day()
    );
    let id = uuid::Uuid::new_v4().simple().to_string();
    let short = &id[..6];
    format!("eval-{date}-{short}")
}

/// Build the proposal-level baseline + shadow JSON blobs.
///
/// Both sides share the same shape so the operator UI can diff them
/// directly. `baseline.passed_cases` is set to `total_cases` because the
/// pre-state is "what the kb was before" — pass/fail is meaningless
/// pre-mutation, but keeping the field consistent simplifies UI code.
fn aggregate(
    eval_run_id: &str,
    kind: EvolutionKind,
    outputs: &[SimulatorOutput],
) -> (Value, Value) {
    let total = outputs.len();
    let passed = outputs.iter().filter(|o| o.passed).count();
    let failed_names: Vec<&str> = outputs
        .iter()
        .filter(|o| !o.passed)
        .map(|o| o.case_name.as_str())
        .collect();

    let mut latencies: Vec<u64> = outputs.iter().map(|o| o.latency_ms).collect();
    latencies.sort_unstable();
    let p50 = percentile(&latencies, 50);
    let p95 = percentile(&latencies, 95);

    let pass_rate = if total == 0 {
        0.0
    } else {
        passed as f64 / total as f64
    };

    let per_case_shadow: Vec<Value> = outputs
        .iter()
        .map(|o| {
            json!({
                "name": o.case_name,
                "passed": o.passed,
                "latency_ms": o.latency_ms,
                "error": o.error,
                "metrics": Value::Object(o.shadow.clone()),
            })
        })
        .collect();

    let per_case_baseline: Vec<Value> = outputs
        .iter()
        .map(|o| {
            json!({
                "name": o.case_name,
                "passed": true,
                "latency_ms": 0,
                "error": null,
                "metrics": Value::Object(o.baseline.clone()),
            })
        })
        .collect();

    let shadow = json!({
        "eval_run_id": eval_run_id,
        "kind": kind.as_str(),
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": failed_names,
        "pass_rate": pass_rate,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "per_case_shadow": per_case_shadow,
    });

    let baseline = json!({
        "eval_run_id": eval_run_id,
        "kind": kind.as_str(),
        "total_cases": total,
        "passed_cases": total,
        "failed_cases": Vec::<&str>::new(),
        "pass_rate": if total == 0 { 0.0 } else { 1.0 },
        "p50_latency_ms": 0,
        "p95_latency_ms": 0,
        "per_case_shadow": per_case_baseline,
    });

    (baseline, shadow)
}

/// Nearest-rank percentile on a pre-sorted slice. Empty → 0.
fn percentile(sorted: &[u64], p: u8) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let idx = ((p as f64 / 100.0) * sorted.len() as f64).ceil() as usize;
    let idx = idx.saturating_sub(1).min(sorted.len() - 1);
    sorted[idx]
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use corlinman_evolution::{
        EvolutionProposal, EvolutionStatus, EvolutionStore,
    };
    use serde_json::json;
    use std::path::Path;
    use tempfile::TempDir;

    use crate::eval::EvalCase;
    use crate::simulator::{SimulatorError, SimulatorOutput};

    /// Pretend simulator: returns a deterministic merge. Real
    /// `MemoryOpSimulator` lands in parallel; the runner is generic.
    struct MockSimulator {
        kind: EvolutionKind,
    }

    #[async_trait]
    impl KindSimulator for MockSimulator {
        fn kind(&self) -> EvolutionKind {
            self.kind
        }

        async fn simulate(
            &self,
            case: &EvalCase,
            _kb: &Path,
        ) -> Result<SimulatorOutput, SimulatorError> {
            Ok(SimulatorOutput {
                case_name: case.name.clone(),
                passed: true,
                baseline: json!({"chunks_total": 0})
                    .as_object()
                    .cloned()
                    .unwrap(),
                shadow: json!({"chunks_total": 0, "rows_merged": 1})
                    .as_object()
                    .cloned()
                    .unwrap(),
                latency_ms: 5,
                error: None,
            })
        }
    }

    async fn fresh_store() -> (TempDir, EvolutionStore, ProposalsRepo) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();
        let repo = ProposalsRepo::new(store.pool().clone());
        (tmp, store, repo)
    }

    async fn write_eval_set(dir: &Path, kind: EvolutionKind) {
        let kind_dir = dir.join(kind.as_str());
        tokio::fs::create_dir_all(&kind_dir).await.unwrap();
        let body = r#"
description: mock case
kb_seed: []
proposal:
  target: "merge_chunks:1,2"
  reasoning: "mock"
  risk: high
expected:
  outcome: no_op
"#;
        tokio::fs::write(kind_dir.join("case-001.yaml"), body)
            .await
            .unwrap();
    }

    fn proposal(id: &str, kind: EvolutionKind, risk: EvolutionRisk) -> EvolutionProposal {
        EvolutionProposal {
            id: ProposalId::new(id),
            kind,
            target: "merge_chunks:1,2".into(),
            diff: String::new(),
            reasoning: "fixture".into(),
            risk,
            budget_cost: 0,
            status: EvolutionStatus::Pending,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
        }
    }

    #[tokio::test]
    async fn run_once_processes_pending_high_risk() {
        let (tmp, _store, repo) = fresh_store().await;
        let pid = ProposalId::new("p-1");
        repo.insert(&proposal("p-1", EvolutionKind::MemoryOp, EvolutionRisk::High))
            .await
            .unwrap();

        let eval_dir = tmp.path().join("eval");
        write_eval_set(&eval_dir, EvolutionKind::MemoryOp).await;

        let mut runner = ShadowRunner::new(
            repo.clone(),
            tmp.path().join("kb-missing.sqlite"), // bootstrap empty kb
            eval_dir,
        );
        runner.register_simulator(Arc::new(MockSimulator {
            kind: EvolutionKind::MemoryOp,
        }));

        let summary = runner.run_once().await;
        assert_eq!(summary.proposals_claimed, 1);
        assert_eq!(summary.proposals_completed, 1);
        assert_eq!(summary.cases_run, 1);

        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::ShadowDone);

        // shadow_metrics + baseline + eval_run_id all populated.
        let row: (Option<String>, Option<String>, Option<String>) = sqlx::query_as(
            "SELECT shadow_metrics, baseline_metrics_json, eval_run_id
                 FROM evolution_proposals WHERE id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(_store.pool())
        .await
        .unwrap();
        let shadow: Value = serde_json::from_str(&row.0.unwrap()).unwrap();
        assert_eq!(shadow["total_cases"], 1);
        assert_eq!(shadow["passed_cases"], 1);
        assert!(row.1.is_some(), "baseline metrics persisted");
        assert!(row.2.unwrap().starts_with("eval-"));
    }

    #[tokio::test]
    async fn run_once_skips_low_risk() {
        let (tmp, _store, repo) = fresh_store().await;
        let pid = ProposalId::new("p-low");
        repo.insert(&proposal("p-low", EvolutionKind::MemoryOp, EvolutionRisk::Low))
            .await
            .unwrap();
        let eval_dir = tmp.path().join("eval");
        write_eval_set(&eval_dir, EvolutionKind::MemoryOp).await;

        let mut runner =
            ShadowRunner::new(repo.clone(), tmp.path().join("kb.sqlite"), eval_dir);
        runner.register_simulator(Arc::new(MockSimulator {
            kind: EvolutionKind::MemoryOp,
        }));

        let summary = runner.run_once().await;
        assert_eq!(summary.proposals_claimed, 0);

        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Pending);
    }

    #[tokio::test]
    async fn run_once_handles_missing_eval_set() {
        let (tmp, _store, repo) = fresh_store().await;
        let pid = ProposalId::new("p-noeval");
        repo.insert(&proposal(
            "p-noeval",
            EvolutionKind::MemoryOp,
            EvolutionRisk::High,
        ))
        .await
        .unwrap();

        // eval_set_dir exists but has no per-kind subdir → MissingDir.
        let eval_dir = tmp.path().join("eval");
        tokio::fs::create_dir_all(&eval_dir).await.unwrap();

        let mut runner = ShadowRunner::new(repo.clone(), tmp.path().join("kb.sqlite"), eval_dir);
        runner.register_simulator(Arc::new(MockSimulator {
            kind: EvolutionKind::MemoryOp,
        }));

        let summary = runner.run_once().await;
        assert_eq!(summary.proposals_claimed, 1);
        assert_eq!(summary.proposals_completed, 1);
        assert_eq!(summary.cases_run, 0);

        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::ShadowDone);

        let row: (Option<String>,) =
            sqlx::query_as("SELECT eval_run_id FROM evolution_proposals WHERE id = ?")
                .bind(pid.as_str())
                .fetch_one(_store.pool())
                .await
                .unwrap();
        assert_eq!(row.0.as_deref(), Some("no-eval-set"));
    }

    #[tokio::test]
    async fn run_once_skips_no_simulator_registered() {
        let (tmp, _store, repo) = fresh_store().await;
        let pid = ProposalId::new("p-skill");
        repo.insert(&proposal(
            "p-skill",
            EvolutionKind::SkillUpdate,
            EvolutionRisk::High,
        ))
        .await
        .unwrap();

        let eval_dir = tmp.path().join("eval");
        write_eval_set(&eval_dir, EvolutionKind::MemoryOp).await;

        let mut runner = ShadowRunner::new(repo.clone(), tmp.path().join("kb.sqlite"), eval_dir);
        // Only memory_op registered; skill_update has no handler.
        runner.register_simulator(Arc::new(MockSimulator {
            kind: EvolutionKind::MemoryOp,
        }));

        let summary = runner.run_once().await;
        assert_eq!(summary.proposals_claimed, 0);

        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Pending);
    }
}
