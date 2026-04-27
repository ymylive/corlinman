//! `AutoRollbackMonitor` — orchestrates one pass over applied proposals
//! still inside the grace window.
//!
//! Per `run_once`:
//!
//! 1. Pull `Applied` proposals from `[now - grace_window, now]` via
//!    [`ProposalsRepo::list_applied_in_grace_window`].
//! 2. For each row:
//!    - Resolve the per-kind metric whitelist via
//!      [`crate::metrics::watched_event_kinds`]. Unknown kinds are
//!      skipped (we never auto-revert a kind we don't yet have a signal
//!      contract for; counted as "skipped" not "inspected").
//!    - Load the apply-time baseline via
//!      [`HistoryRepo::latest_for_proposal`] and parse its
//!      `metrics_baseline` into a [`MetricSnapshot`].
//!    - Take a fresh post-apply snapshot via
//!      [`crate::metrics::capture_snapshot`].
//!    - Run [`crate::metrics::compute_delta`] +
//!      [`crate::metrics::breaches_threshold`].
//!    - On breach, call [`Applier::revert`] and update the per-row
//!      counters. Per-row failures degrade into `summary.errors` /
//!      `summary.rollbacks_failed` rather than panicking the whole run.
//! 3. Return a [`RunSummary`] for the CLI / scheduler log line.
//!
//! The monitor never consults the `enabled` flag itself — the CLI
//! binary short-circuits at the entry point so `run_once` is always a
//! "do the work" call. Mirrors the W1-A `ShadowRunner` shape.

use std::sync::Arc;

use corlinman_core::config::{AutoRollbackThresholds, EvolutionAutoRollbackConfig};
use corlinman_evolution::{HistoryRepo, ProposalsRepo, RepoError};
use sqlx::SqlitePool;
use tracing::{debug, info, warn};

use crate::metrics::{
    breaches_threshold, capture_snapshot, compute_delta, watched_event_kinds, MetricSnapshot,
};
use crate::revert::{Applier, RevertError};

/// Counts surfaced by `run_once` so the CLI / scheduler can log a
/// one-line summary (and future Prometheus counters can read off this
/// shape directly).
#[derive(Debug, Default, Clone)]
pub struct RunSummary {
    pub proposals_inspected: usize,
    pub thresholds_breached: usize,
    pub rollbacks_triggered: usize,
    pub rollbacks_succeeded: usize,
    pub rollbacks_failed: usize,
    pub errors: usize,
}

pub struct AutoRollbackMonitor {
    proposals: ProposalsRepo,
    history: HistoryRepo,
    /// `evolution.sqlite` pool — handed straight to `capture_snapshot`
    /// so the monitor owns one source of truth for signal counts.
    evolution_pool: SqlitePool,
    applier: Arc<dyn Applier>,
    grace_window_hours: u32,
    thresholds: AutoRollbackThresholds,
    max_proposals_per_run: usize,
}

impl AutoRollbackMonitor {
    pub fn new(
        proposals: ProposalsRepo,
        history: HistoryRepo,
        evolution_pool: SqlitePool,
        applier: Arc<dyn Applier>,
        config: EvolutionAutoRollbackConfig,
    ) -> Self {
        Self {
            proposals,
            history,
            evolution_pool,
            applier,
            grace_window_hours: config.grace_window_hours,
            thresholds: config.thresholds,
            max_proposals_per_run: 50,
        }
    }

    /// Operator override — primarily for tests + one-off backfills. The
    /// default (50) tracks the engine's per-run proposal cap.
    pub fn with_max_proposals_per_run(mut self, n: usize) -> Self {
        self.max_proposals_per_run = n;
        self
    }

    pub async fn run_once(&self) -> RunSummary {
        let mut summary = RunSummary::default();
        let now_ms = now_ms();

        let candidates = match self
            .proposals
            .list_applied_in_grace_window(
                now_ms,
                self.grace_window_hours,
                self.max_proposals_per_run as i64,
            )
            .await
        {
            Ok(c) => c,
            Err(e) => {
                warn!(error = %e, "auto_rollback: list_applied_in_grace_window failed");
                summary.errors += 1;
                return summary;
            }
        };

        for proposal in candidates {
            // Per-kind whitelist — kinds without one are intentionally
            // unrolled-back rather than declared "fine"; skip silently.
            let watched = watched_event_kinds(proposal.kind);
            if watched.is_empty() {
                debug!(
                    proposal_id = %proposal.id,
                    kind = proposal.kind.as_str(),
                    "auto_rollback: no whitelist for kind {}; skipping",
                    proposal.kind.as_str(),
                );
                continue;
            }

            // Apply-time baseline. Missing here is data corruption: the
            // forward applier wrote the row before flipping status.
            let history = match self.history.latest_for_proposal(&proposal.id).await {
                Ok(h) => h,
                Err(RepoError::NotFound(_)) => {
                    warn!(
                        proposal_id = %proposal.id,
                        "auto_rollback: history missing for applied proposal — corruption"
                    );
                    summary.errors += 1;
                    continue;
                }
                Err(e) => {
                    warn!(
                        proposal_id = %proposal.id,
                        error = %e,
                        "auto_rollback: history fetch failed"
                    );
                    summary.errors += 1;
                    continue;
                }
            };

            // Parse baseline JSON. A malformed baseline must not auto-
            // revert — fail safe and let the operator inspect.
            let baseline: MetricSnapshot =
                match serde_json::from_value(history.metrics_baseline.clone()) {
                    Ok(s) => s,
                    Err(e) => {
                        warn!(
                            proposal_id = %proposal.id,
                            error = %e,
                            "auto_rollback: malformed metrics_baseline JSON; skipping"
                        );
                        summary.errors += 1;
                        continue;
                    }
                };

            let current = match capture_snapshot(
                &self.evolution_pool,
                &proposal.target,
                watched,
                self.thresholds.signal_window_secs,
                now_ms,
            )
            .await
            {
                Ok(s) => s,
                Err(e) => {
                    warn!(
                        proposal_id = %proposal.id,
                        error = %e,
                        "auto_rollback: capture_snapshot failed"
                    );
                    summary.errors += 1;
                    continue;
                }
            };

            let delta = compute_delta(&baseline, &current);
            summary.proposals_inspected += 1;

            let reason = match breaches_threshold(&delta, &self.thresholds) {
                Some(r) => r,
                None => continue,
            };
            summary.thresholds_breached += 1;
            summary.rollbacks_triggered += 1;

            match self.applier.revert(&proposal.id, &reason).await {
                Ok(()) => {
                    summary.rollbacks_succeeded += 1;
                    info!(
                        proposal_id = %proposal.id,
                        kind = proposal.kind.as_str(),
                        reason = %reason,
                        "auto_rollback: revert succeeded"
                    );
                }
                Err(RevertError::NotApplied(status)) => {
                    // Race with operator / a concurrent monitor pass —
                    // benign, log + count as failed-but-not-error.
                    info!(
                        proposal_id = %proposal.id,
                        status = %status,
                        "auto_rollback: revert raced — already not applied"
                    );
                    summary.rollbacks_failed += 1;
                }
                Err(other) => {
                    warn!(
                        proposal_id = %proposal.id,
                        error = %other,
                        "auto_rollback: revert failed"
                    );
                    summary.rollbacks_failed += 1;
                }
            }
        }

        summary
    }
}

/// Shared `now_ms` helper. `corlinman-evolution::now_ms` lives in a
/// separate module path; duplicating here keeps this crate from leaning
/// on a private API.
fn now_ms() -> i64 {
    let nanos = time::OffsetDateTime::now_utc().unix_timestamp_nanos();
    (nanos / 1_000_000) as i64
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use corlinman_evolution::{
        EvolutionHistory, EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus,
        EvolutionStore, ProposalId,
    };
    use serde_json::json;
    use std::sync::Mutex;
    use tempfile::TempDir;

    /// Mock `Applier`: records every call, returns whatever the test
    /// pre-loaded into `result`. Mirrors the shape from `revert::tests`.
    struct MockApplier {
        calls: Mutex<Vec<(String, String)>>,
        result: Mutex<Result<(), RevertError>>,
    }

    impl MockApplier {
        fn ok() -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(Vec::new()),
                result: Mutex::new(Ok(())),
            })
        }
        fn err(e: RevertError) -> Arc<Self> {
            Arc::new(Self {
                calls: Mutex::new(Vec::new()),
                result: Mutex::new(Err(e)),
            })
        }
        fn calls(&self) -> Vec<(String, String)> {
            self.calls.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl Applier for MockApplier {
        async fn revert(&self, id: &ProposalId, reason: &str) -> Result<(), RevertError> {
            self.calls
                .lock()
                .unwrap()
                .push((id.0.clone(), reason.to_string()));
            match &*self.result.lock().unwrap() {
                Ok(()) => Ok(()),
                Err(RevertError::NotFound(s)) => Err(RevertError::NotFound(s.clone())),
                Err(RevertError::NotApplied(s)) => Err(RevertError::NotApplied(s.clone())),
                Err(RevertError::HistoryMissing(s)) => Err(RevertError::HistoryMissing(s.clone())),
                Err(RevertError::UnsupportedKind(s)) => {
                    Err(RevertError::UnsupportedKind(s.clone()))
                }
                Err(RevertError::Internal(s)) => Err(RevertError::Internal(s.clone())),
            }
        }
    }

    async fn fresh_store() -> (TempDir, EvolutionStore, ProposalsRepo, HistoryRepo) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();
        let proposals = ProposalsRepo::new(store.pool().clone());
        let history = HistoryRepo::new(store.pool().clone());
        (tmp, store, proposals, history)
    }

    fn sample_config(grace_hours: u32, min_baseline: u32, err_pct: f64) -> EvolutionAutoRollbackConfig {
        EvolutionAutoRollbackConfig {
            enabled: true,
            grace_window_hours: grace_hours,
            thresholds: AutoRollbackThresholds {
                default_err_rate_delta_pct: err_pct,
                default_p95_latency_delta_pct: 25.0,
                signal_window_secs: 1_800,
                min_baseline_signals: min_baseline,
            },
        }
    }

    /// Insert an applied proposal — kind/target/applied_at vary per test.
    async fn seed_applied(
        repo: &ProposalsRepo,
        id: &str,
        kind: EvolutionKind,
        target: &str,
        applied_at_ms: i64,
    ) -> ProposalId {
        let pid = ProposalId::new(id);
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind,
            target: target.into(),
            diff: String::new(),
            reasoning: String::new(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Applied,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: Some(2_000),
            decided_by: Some("auto".into()),
            applied_at: Some(applied_at_ms),
            rollback_of: None,
        })
        .await
        .unwrap();
        pid
    }

    /// Insert a baseline-bearing history row. `baseline_json` is written
    /// verbatim so corrupted-JSON tests can override the shape.
    async fn seed_history(
        history: &HistoryRepo,
        proposal_id: &ProposalId,
        target: &str,
        baseline_json: serde_json::Value,
    ) {
        history
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: proposal_id.clone(),
                kind: EvolutionKind::MemoryOp,
                target: target.into(),
                before_sha: "x".into(),
                after_sha: "y".into(),
                inverse_diff: r#"{"action":"restore_chunk","content":"x","namespace":"general","file_id":1,"chunk_index":0}"#.into(),
                metrics_baseline: baseline_json,
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();
    }

    /// Insert N evolution_signals rows (`tool.call.failed`, error severity)
    /// at `now_ms - 1s` so they fall inside the default 1800s window.
    async fn seed_signals(pool: &SqlitePool, target: &str, n: usize, observed_at: i64) {
        for _ in 0..n {
            sqlx::query(
                r#"INSERT INTO evolution_signals
                     (event_kind, target, severity, payload_json, observed_at)
                   VALUES ('tool.call.failed', ?, 'error', '{}', ?)"#,
            )
            .bind(target)
            .bind(observed_at)
            .execute(pool)
            .await
            .unwrap();
        }
    }

    fn baseline_json(target: &str, count: u64) -> serde_json::Value {
        json!({
            "target": target,
            "captured_at_ms": 0,
            "window_secs": 1_800,
            "counts": {
                "tool.call.failed": count,
                "search.recall.dropped": 0,
            }
        })
    }

    #[tokio::test]
    async fn run_once_no_proposals_in_window() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        let mock = MockApplier::ok();
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.proposals_inspected, 0);
        assert_eq!(summary.thresholds_breached, 0);
        assert_eq!(summary.rollbacks_triggered, 0);
        assert_eq!(summary.rollbacks_succeeded, 0);
        assert_eq!(summary.rollbacks_failed, 0);
        assert_eq!(summary.errors, 0);
        assert!(mock.calls().is_empty());
    }

    #[tokio::test]
    async fn run_once_skips_kind_without_whitelist() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        // tag_rebalance has no whitelist — must not be inspected.
        let pid = seed_applied(
            &proposals,
            "evol-skip-tag",
            EvolutionKind::TagRebalance,
            "tag_tree",
            now_ms(),
        )
        .await;
        // Insert a history row anyway; the monitor never reaches it.
        history
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: pid.clone(),
                kind: EvolutionKind::TagRebalance,
                target: "tag_tree".into(),
                before_sha: "x".into(),
                after_sha: "y".into(),
                inverse_diff: "{}".into(),
                metrics_baseline: json!({}),
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();

        let mock = MockApplier::ok();
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.proposals_inspected, 0, "kind without whitelist must skip");
        assert_eq!(summary.errors, 0);
        assert!(mock.calls().is_empty());
    }

    #[tokio::test]
    async fn run_once_no_breach_keeps_proposal_applied() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        let now = now_ms();
        let target = "delete_chunk:42";
        let pid = seed_applied(
            &proposals,
            "evol-no-breach",
            EvolutionKind::MemoryOp,
            target,
            now,
        )
        .await;
        // High baseline (50) but no fresh signals — delta is negative.
        seed_history(&history, &pid, target, baseline_json(target, 50)).await;

        let mock = MockApplier::ok();
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.proposals_inspected, 1);
        assert_eq!(summary.thresholds_breached, 0);
        assert_eq!(summary.rollbacks_triggered, 0);
        assert!(mock.calls().is_empty(), "no breach → no revert call");
    }

    #[tokio::test]
    async fn run_once_breach_triggers_revert() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        let now = now_ms();
        let target = "delete_chunk:7";
        let pid = seed_applied(
            &proposals,
            "evol-breach",
            EvolutionKind::MemoryOp,
            target,
            now,
        )
        .await;
        // Baseline 10 → seed 100 fresh error signals → +900%.
        seed_history(&history, &pid, target, baseline_json(target, 10)).await;
        seed_signals(store.pool(), target, 100, now - 1_000).await;

        let mock = MockApplier::ok();
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.proposals_inspected, 1);
        assert_eq!(summary.thresholds_breached, 1);
        assert_eq!(summary.rollbacks_triggered, 1);
        assert_eq!(summary.rollbacks_succeeded, 1);
        assert_eq!(summary.rollbacks_failed, 0);

        let calls = mock.calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "evol-breach");
        assert!(
            calls[0].1.contains("breaches threshold"),
            "reason should match the metrics summary; got {:?}",
            calls[0].1,
        );
    }

    #[tokio::test]
    async fn run_once_handles_revert_already_rolled_back() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        let now = now_ms();
        let target = "delete_chunk:11";
        let pid = seed_applied(
            &proposals,
            "evol-race",
            EvolutionKind::MemoryOp,
            target,
            now,
        )
        .await;
        seed_history(&history, &pid, target, baseline_json(target, 10)).await;
        seed_signals(store.pool(), target, 100, now - 1_000).await;

        let mock = MockApplier::err(RevertError::NotApplied("rolled_back".into()));
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.thresholds_breached, 1);
        assert_eq!(summary.rollbacks_triggered, 1);
        assert_eq!(summary.rollbacks_succeeded, 0);
        assert_eq!(summary.rollbacks_failed, 1);
        // Critically: not a panic, not a top-level error counter bump.
        assert_eq!(summary.errors, 0);
    }

    #[tokio::test]
    async fn run_once_corrupted_baseline_json_does_not_revert() {
        let (_tmp, store, proposals, history) = fresh_store().await;
        let now = now_ms();
        let target = "delete_chunk:99";
        let pid = seed_applied(
            &proposals,
            "evol-bad-json",
            EvolutionKind::MemoryOp,
            target,
            now,
        )
        .await;
        // `target` is a string, not an object — parse must fail.
        seed_history(&history, &pid, target, json!("totally-not-a-snapshot")).await;
        seed_signals(store.pool(), target, 100, now - 1_000).await;

        let mock = MockApplier::ok();
        let monitor = AutoRollbackMonitor::new(
            proposals,
            history,
            store.pool().clone(),
            mock.clone(),
            sample_config(72, 5, 50.0),
        );
        let summary = monitor.run_once().await;
        assert_eq!(summary.errors, 1);
        assert_eq!(summary.proposals_inspected, 0, "skipped before delta");
        assert!(mock.calls().is_empty(), "corruption must not auto-revert");
    }
}
