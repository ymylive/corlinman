//! Cross-component smoke test: real `ShadowRunner` + real
//! `MemoryOpSimulator` + the 4 hand-crafted fixtures under
//! `tests/fixtures/eval/memory_op/`.
//!
//! `runner::tests::run_once_processes_pending_high_risk` covers the
//! orchestration with a `MockSimulator` + an inline YAML case. This
//! file proves that the *real* simulator behaves correctly across all
//! four shipped fixtures — so a regression in either piece (parse rule,
//! NoOp detection, aggregation shape) shows up as one obvious failure
//! here instead of leaking past Step 3 acceptance.
//!
//! End-state assertion: a single high-risk memory_op proposal flows
//! `Pending → ShadowDone`, with `shadow_metrics.pass_rate = 1.0` and
//! `failed_cases = []`. Anything less means a fixture-vs-simulator
//! contract drift the operator would see in production.

use std::path::PathBuf;
use std::sync::Arc;

use corlinman_evolution::{
    EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore, ProposalId,
    ProposalsRepo,
};
use corlinman_shadow_tester::simulator::MemoryOpSimulator;
use corlinman_shadow_tester::{KindSimulator, ShadowRunner};
use tempfile::TempDir;

#[tokio::test]
async fn shadow_run_passes_all_real_memory_op_fixtures() {
    let tmp = TempDir::new().unwrap();
    let evolution_path = tmp.path().join("evolution.sqlite");

    // 1. Stand up evolution.sqlite via the production open path so
    //    schema + v0.3 columns are present.
    let store = EvolutionStore::open(&evolution_path).await.unwrap();
    let proposals = ProposalsRepo::new(store.pool().clone());

    // 2. Seed one high-risk memory_op proposal — this is the row the
    //    runner should claim, shadow, and mark `shadow_done`.
    let id = ProposalId::new("evol-test-shadow-real-001");
    proposals
        .insert(&EvolutionProposal {
            id: id.clone(),
            kind: EvolutionKind::MemoryOp,
            target: "merge_chunks:1,2".into(),
            diff: String::new(),
            reasoning: "real-fixture integration test".into(),
            risk: EvolutionRisk::High,
            budget_cost: 1,
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
            metadata: None,
        })
        .await
        .unwrap();

    // 3. Wire the runner against the crate's own fixture tree. kb_path
    //    points at a non-existent file: the runner's fallback creates
    //    an empty schema in the per-case tempdir before the simulator
    //    reopens it.
    let fixtures_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("eval");
    let kb_path = tmp.path().join("kb-does-not-exist.sqlite");

    let mut runner = ShadowRunner::new(proposals.clone(), kb_path, fixtures_root);
    runner.register_simulator(Arc::new(MemoryOpSimulator) as Arc<dyn KindSimulator>);

    let summary = runner.run_once().await;

    // 4. Orchestration assertions — exactly one proposal claimed +
    //    completed, no errors.
    assert_eq!(summary.proposals_claimed, 1, "should claim our seeded row");
    assert_eq!(summary.proposals_completed, 1, "should finish the run");
    assert_eq!(summary.proposals_failed, 0, "no orchestration failures");
    assert_eq!(summary.errors, 0, "no errors logged");
    assert_eq!(summary.cases_run, 4, "all four memory_op fixtures execute");

    // 5. Row-level assertions — status terminal, all v0.3 columns
    //    populated, pass_rate is the contract: 4/4 fixtures pass under
    //    the deterministic v0.3 simulator. A drop here means either a
    //    fixture or the simulator drifted.
    let after = proposals.get(&id).await.unwrap();
    assert_eq!(after.status, EvolutionStatus::ShadowDone);

    let pool = store.pool();
    let row: (Option<String>, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT eval_run_id, baseline_metrics_json, shadow_metrics
           FROM evolution_proposals WHERE id = ?",
    )
    .bind(id.as_str())
    .fetch_one(pool)
    .await
    .unwrap();

    let eval_run_id = row.0.expect("eval_run_id populated");
    assert!(
        eval_run_id.starts_with("eval-"),
        "eval_run_id should follow `eval-<...>` convention, got {eval_run_id:?}",
    );

    let baseline: serde_json::Value =
        serde_json::from_str(&row.1.expect("baseline_metrics_json populated")).unwrap();
    let shadow: serde_json::Value =
        serde_json::from_str(&row.2.expect("shadow_metrics populated")).unwrap();

    // Fields present on both blobs.
    for key in [
        "eval_run_id",
        "kind",
        "total_cases",
        "pass_rate",
        "p95_latency_ms",
    ] {
        assert!(baseline.get(key).is_some(), "baseline missing {key}");
        assert!(shadow.get(key).is_some(), "shadow missing {key}");
    }

    // The contract: every shipped fixture passes against the
    // deterministic simulator after the case-002 fix. A regression
    // here is visible to operators as a sub-100% pass rate on every
    // shadowed proposal — this assertion guards against that.
    assert_eq!(
        shadow["total_cases"].as_u64().unwrap(),
        4,
        "all 4 fixtures must run",
    );
    assert_eq!(
        shadow["passed_cases"].as_u64().unwrap(),
        4,
        "all 4 fixtures must pass under v0.3 deterministic simulator",
    );
    assert_eq!(shadow["pass_rate"].as_f64().unwrap(), 1.0);
    assert_eq!(
        shadow["failed_cases"].as_array().unwrap().len(),
        0,
        "no fixture should fail; got: {:?}",
        shadow["failed_cases"],
    );
    assert_eq!(shadow["kind"].as_str().unwrap(), "memory_op");
}
