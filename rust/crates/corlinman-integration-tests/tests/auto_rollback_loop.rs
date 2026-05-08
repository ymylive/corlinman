//! Cross-component e2e for the AutoRollback loop. Exercises the wires
//! that no single-crate test can cover by itself:
//!
//!   evolution.sqlite ← apply → history.metrics_baseline
//!                              ↓
//!         insert spike signals into evolution_signals
//!                              ↓
//!         monitor.run_once → AutoRollbackApplier::revert
//!                              ↓
//!         proposal.status = RolledBack + chunks restored in kb
//!
//! These tests rely on the *real* `EvolutionApplier::apply` to populate
//! `evolution_history.inverse_diff` — hand-crafting that JSON in tests
//! would couple the assertion to the exact shape and would silently
//! drift from production. Better to exercise the contract.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use corlinman_auto_rollback::{Applier, AutoRollbackMonitor};
use corlinman_core::config::{AutoRollbackThresholds, EvolutionAutoRollbackConfig};
use corlinman_evolution::{
    EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore, HistoryRepo,
    ProposalId, ProposalsRepo,
};
use corlinman_gateway::evolution_applier::EvolutionApplier;
use corlinman_vector::SqliteStore;
use tempfile::TempDir;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

/// Spin up tempdir evolution + kb stores and return them along with the
/// `now` timestamp the test uses as its anchor. Real `now_ms()` is used
/// throughout — `signal_window_secs` (default 1800) gives us plenty of
/// slack.
async fn setup() -> (TempDir, Arc<EvolutionStore>, Arc<SqliteStore>, i64) {
    let tmp = TempDir::new().unwrap();
    let evol_path = tmp.path().join("evolution.sqlite");
    let kb_path = tmp.path().join("kb.sqlite");
    let evol = Arc::new(EvolutionStore::open(&evol_path).await.unwrap());
    let kb = Arc::new(SqliteStore::open(&kb_path).await.unwrap());
    let now = now_ms();
    (tmp, evol, kb, now)
}

/// Seed two adjacent chunks under a single file row. Returns the chunk
/// ids — the proposal's `merge_chunks:` target uses these.
async fn seed_two_chunks(kb: &SqliteStore) -> (i64, i64) {
    let file_id: i64 = sqlx::query_scalar(
        "INSERT INTO files(path, diary_name, checksum, mtime, size, updated_at)
         VALUES ('fx-rollback.md', 'fixture', 'h', 0, 0, 0) RETURNING id",
    )
    .fetch_one(kb.pool())
    .await
    .unwrap();
    let id_a: i64 = sqlx::query_scalar(
        "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace)
         VALUES (?, 0, 'first chunk for rollback fixture', NULL, 'general') RETURNING id",
    )
    .bind(file_id)
    .fetch_one(kb.pool())
    .await
    .unwrap();
    let id_b: i64 = sqlx::query_scalar(
        "INSERT INTO chunks(file_id, chunk_index, content, vector, namespace)
         VALUES (?, 1, 'second chunk for rollback fixture', NULL, 'general') RETURNING id",
    )
    .bind(file_id)
    .fetch_one(kb.pool())
    .await
    .unwrap();
    (id_a, id_b)
}

/// Insert N error-severity signals at staggered timestamps inside the
/// monitor's `signal_window_secs` window. Anchored at `at_ms` so callers
/// place baseline signals before apply and spike signals after.
async fn seed_error_signals(
    evol: &EvolutionStore,
    target: &str,
    event_kind: &str,
    n: usize,
    at_ms: i64,
) {
    for i in 0..n {
        sqlx::query(
            "INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json,
                  trace_id, session_id, observed_at)
             VALUES (?, ?, 'error', '{}', NULL, NULL, ?)",
        )
        .bind(event_kind)
        .bind(target)
        .bind(at_ms + i as i64)
        .execute(evol.pool())
        .await
        .unwrap();
    }
}

/// Insert an Approved memory_op proposal targeting the supplied chunk
/// pair, then forward-apply it via the real `EvolutionApplier`. After
/// this returns, the kb has the smaller chunk only and the history row
/// has a populated `inverse_diff` — the contract Step 3 revert needs.
async fn approved_then_applied(
    evol: Arc<EvolutionStore>,
    kb: Arc<SqliteStore>,
    proposals: &ProposalsRepo,
    target_chunks: (i64, i64),
    thresholds: AutoRollbackThresholds,
) -> ProposalId {
    let id = ProposalId::new("evol-test-rollback-001");
    let target = format!("merge_chunks:{},{}", target_chunks.0, target_chunks.1);
    proposals
        .insert(&EvolutionProposal {
            id: id.clone(),
            kind: EvolutionKind::MemoryOp,
            target: target.clone(),
            diff: String::new(),
            reasoning: "auto-rollback e2e".into(),
            risk: EvolutionRisk::High,
            budget_cost: 1,
            status: EvolutionStatus::Approved,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: Some(2_000),
            decided_by: Some("test".into()),
            applied_at: None,
            rollback_of: None,
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
            metadata: None,
        })
        .await
        .unwrap();

    // Phase 3-2B added a `skills_dir` param. memory_op doesn't read it,
    // so a throwaway tempdir is fine for the integration loop.
    let skills_dir = std::env::temp_dir().join("corlinman-itest-skills");
    let applier = EvolutionApplier::new(evol, kb, thresholds, skills_dir);
    applier
        .apply(&id)
        .await
        .expect("forward apply must succeed for the e2e to be meaningful");
    id
}

#[tokio::test]
async fn auto_rollback_breach_triggers_revert() {
    let (_tmp, evol, kb, now) = setup().await;
    let (id_a, id_b) = seed_two_chunks(&kb).await;
    assert_eq!(id_a, 1);
    assert_eq!(id_b, 2);
    let target = format!("merge_chunks:{id_a},{id_b}");

    // Baseline signals must land BEFORE apply so the captured snapshot
    // sees them. 10 satisfies `min_baseline_signals = 5` and gives the
    // ratio math a non-zero denominator.
    seed_error_signals(&evol, &target, "tool.call.failed", 10, now - 600_000).await;

    let thresholds = AutoRollbackThresholds::default();
    let proposals = ProposalsRepo::new(evol.pool().clone());
    let history = HistoryRepo::new(evol.pool().clone());
    let id = approved_then_applied(
        evol.clone(),
        kb.clone(),
        &proposals,
        (id_a, id_b),
        thresholds.clone(),
    )
    .await;

    // Spike signals AFTER apply — these only show up in the monitor's
    // current snapshot, not in the apply-time baseline. 50 over a
    // baseline of 10 = +400%, well above default 50% threshold.
    seed_error_signals(&evol, &target, "tool.call.failed", 50, now - 60_000).await;

    let cfg = EvolutionAutoRollbackConfig {
        enabled: true,
        grace_window_hours: 72,
        thresholds: thresholds.clone(),
    };
    let skills_dir = std::env::temp_dir().join("corlinman-itest-skills");
    let applier = Arc::new(EvolutionApplier::new(
        evol.clone(),
        kb.clone(),
        thresholds,
        skills_dir,
    )) as Arc<dyn Applier>;
    let monitor = AutoRollbackMonitor::new(
        proposals.clone(),
        history.clone(),
        evol.pool().clone(),
        applier,
        cfg,
    );

    let summary = monitor.run_once().await;

    assert_eq!(summary.proposals_inspected, 1);
    assert_eq!(summary.thresholds_breached, 1);
    assert_eq!(summary.rollbacks_triggered, 1);
    assert_eq!(summary.rollbacks_succeeded, 1, "rollback should succeed");
    assert_eq!(summary.rollbacks_failed, 0);
    assert_eq!(summary.errors, 0);

    // Proposal terminal in RolledBack with audit fields populated.
    let after = proposals.get(&id).await.unwrap();
    assert_eq!(after.status, EvolutionStatus::RolledBack);

    let row: (Option<i64>, Option<String>) = sqlx::query_as(
        "SELECT auto_rollback_at, auto_rollback_reason
           FROM evolution_proposals WHERE id = ?",
    )
    .bind(id.as_str())
    .fetch_one(evol.pool())
    .await
    .unwrap();
    assert!(row.0.is_some(), "auto_rollback_at must be set");
    let reason = row.1.expect("auto_rollback_reason must be set");
    assert!(
        reason.contains("breaches threshold"),
        "reason should describe the breach; got {reason:?}",
    );

    // The losing chunk must be back in the kb after revert.
    let chunk_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chunks")
        .fetch_one(kb.pool())
        .await
        .unwrap();
    assert_eq!(
        chunk_count, 2,
        "revert should restore both chunks (kb had 2 → apply removed 1 → revert restores 2)",
    );
}

#[tokio::test]
async fn auto_rollback_no_breach_keeps_state() {
    let (_tmp, evol, kb, now) = setup().await;
    let (id_a, id_b) = seed_two_chunks(&kb).await;
    let target = format!("merge_chunks:{id_a},{id_b}");

    seed_error_signals(&evol, &target, "tool.call.failed", 10, now - 600_000).await;

    let thresholds = AutoRollbackThresholds::default();
    let proposals = ProposalsRepo::new(evol.pool().clone());
    let history = HistoryRepo::new(evol.pool().clone());
    let id = approved_then_applied(
        evol.clone(),
        kb.clone(),
        &proposals,
        (id_a, id_b),
        thresholds.clone(),
    )
    .await;

    // Tiny spike — 2 over a baseline of 10 = +20%, below the default
    // 50% threshold. Quiet-target guard already satisfied (baseline
    // 10 ≥ 5), so the threshold itself is the gating factor here.
    seed_error_signals(&evol, &target, "tool.call.failed", 2, now - 60_000).await;

    let cfg = EvolutionAutoRollbackConfig {
        enabled: true,
        grace_window_hours: 72,
        thresholds: thresholds.clone(),
    };
    let skills_dir = std::env::temp_dir().join("corlinman-itest-skills");
    let applier = Arc::new(EvolutionApplier::new(
        evol.clone(),
        kb.clone(),
        thresholds,
        skills_dir,
    )) as Arc<dyn Applier>;
    let monitor = AutoRollbackMonitor::new(
        proposals.clone(),
        history.clone(),
        evol.pool().clone(),
        applier,
        cfg,
    );

    let summary = monitor.run_once().await;

    assert_eq!(summary.proposals_inspected, 1);
    assert_eq!(
        summary.thresholds_breached, 0,
        "20% delta must not breach 50% threshold"
    );
    assert_eq!(summary.rollbacks_triggered, 0);
    assert_eq!(summary.rollbacks_succeeded, 0);
    assert_eq!(summary.errors, 0);

    let after = proposals.get(&id).await.unwrap();
    assert_eq!(
        after.status,
        EvolutionStatus::Applied,
        "no breach → proposal stays Applied",
    );
}
