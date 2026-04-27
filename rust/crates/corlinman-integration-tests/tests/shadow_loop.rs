//! Phase 3 W1-A Step 4 (part 2) — cross-crate shadow-loop e2e.
//!
//! Proves the components that ship together survive being wired up
//! across crate boundaries:
//!
//! ```text
//! evolution_proposals row (Pending)
//!   → ShadowRunner.run_once
//!   → MemoryOpSimulator runs against an inline eval-set fixture
//!   → evolution_proposals row (ShadowDone) with shadow_metrics +
//!     baseline_metrics_json + eval_run_id populated
//! ```
//!
//! Single-crate coverage already exists at
//! `crates/corlinman-shadow-tester/tests/integration_real_fixtures.rs` —
//! that test pins the simulator-vs-fixtures contract. This file pins the
//! cross-crate wire-up: the evolution crate's repo + the shadow-tester
//! crate's runner observed together, so a future API drift between them
//! shows up as one obvious failure here.
//!
//! ### Fixture path strategy
//!
//! Two options were on the table:
//!   (a) compute a relative path from this crate's `CARGO_MANIFEST_DIR`
//!       to the sibling shadow-tester crate's `tests/fixtures/eval/`;
//!   (b) write a minimal eval-set fixture into a tempdir at test time.
//!
//! We use (b). It keeps this test self-contained (changes to the
//! shadow-tester crate's fixture set never break this test) and exercises
//! the `kb_seed` SQL replay path, which is what we actually want to
//! confirm works across crates. (a) would couple the integration-tests
//! crate to a sibling crate's directory layout, which is fragile.

use std::path::Path;
use std::sync::Arc;

use corlinman_evolution::{
    EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore, ProposalId,
    ProposalsRepo,
};
use corlinman_shadow_tester::simulator::MemoryOpSimulator;
use corlinman_shadow_tester::{KindSimulator, ShadowRunner};
use serde_json::Value;
use tempfile::TempDir;

/// Minimal `memory_op` eval-set fixture. Mirrors
/// `case-001-near-duplicate-merge.yaml` from the shadow-tester crate but
/// inline, so this test owns its own contract surface.
const INLINE_MEMORY_OP_CASE: &str = r#"
description: "inline integration fixture: simple merge"
kb_seed:
  - "INSERT INTO files(id, path, diary_name, checksum, mtime, size) VALUES (1, 'fx.md', 'fixture', 'h', 0, 0);"
  - "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (1, 1, 0, 'first chunk content here', 'general');"
  - "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (2, 1, 1, 'second chunk content here', 'general');"
proposal:
  target: "merge_chunks:1,2"
  reasoning: "integration test"
  risk: high
expected:
  outcome: merged
  rows_merged: 1
  surviving_chunk_id: 1
"#;

/// Write `INLINE_MEMORY_OP_CASE` into `<eval_dir>/memory_op/case-001.yaml`.
async fn write_inline_eval_set(eval_dir: &Path) {
    let kind_dir = eval_dir.join("memory_op");
    tokio::fs::create_dir_all(&kind_dir).await.unwrap();
    tokio::fs::write(kind_dir.join("case-001.yaml"), INLINE_MEMORY_OP_CASE)
        .await
        .unwrap();
}

/// Build a `Pending` proposal with `id`/`kind`/`risk` and otherwise-empty
/// fields. Tests vary only those three knobs.
fn pending_proposal(id: &str, kind: EvolutionKind, risk: EvolutionRisk) -> EvolutionProposal {
    EvolutionProposal {
        id: ProposalId::new(id),
        kind,
        target: "merge_chunks:1,2".into(),
        diff: String::new(),
        reasoning: "shadow-loop integration test".into(),
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

/// Open `evolution.sqlite` under `tmp` via the production schema path
/// and return a repo handle on its pool.
async fn fresh_store(tmp: &TempDir) -> (EvolutionStore, ProposalsRepo) {
    let path = tmp.path().join("evolution.sqlite");
    let store = EvolutionStore::open(&path).await.unwrap();
    let repo = ProposalsRepo::new(store.pool().clone());
    (store, repo)
}

// ---------------------------------------------------------------------------
// Test A — happy path across crate boundaries.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn shadow_loop_high_risk_memory_op_passes_to_shadow_done() {
    let tmp = TempDir::new().unwrap();
    let (store, repo) = fresh_store(&tmp).await;

    // 1. The proposal that should be claimed and shadowed.
    let target_id = ProposalId::new("evol-shadow-loop-A-high-mem");
    repo.insert(&pending_proposal(
        target_id.as_str(),
        EvolutionKind::MemoryOp,
        EvolutionRisk::High,
    ))
    .await
    .unwrap();

    // 2. Control: low-risk memory_op — must remain Pending (shadow gate
    //    only fires on medium/high).
    let low_id = ProposalId::new("evol-shadow-loop-A-low-mem");
    repo.insert(&pending_proposal(
        low_id.as_str(),
        EvolutionKind::MemoryOp,
        EvolutionRisk::Low,
    ))
    .await
    .unwrap();

    // 3. Control: high-risk skill_update — must remain Pending (no
    //    simulator registered for that kind).
    let skill_id = ProposalId::new("evol-shadow-loop-A-skill");
    repo.insert(&pending_proposal(
        skill_id.as_str(),
        EvolutionKind::SkillUpdate,
        EvolutionRisk::High,
    ))
    .await
    .unwrap();

    // 4. Inline eval-set fixture under tempdir.
    let eval_dir = tmp.path().join("eval");
    write_inline_eval_set(&eval_dir).await;

    // 5. Wire the runner. kb_path points at a missing file so the
    //    runner's empty-kb bootstrap path is exercised — that's the
    //    realistic state for an integration test.
    let mut runner =
        ShadowRunner::new(repo.clone(), tmp.path().join("kb-missing.sqlite"), eval_dir);
    runner.register_simulator(Arc::new(MemoryOpSimulator) as Arc<dyn KindSimulator>);

    let summary = runner.run_once().await;

    // Orchestration: only the high-risk memory_op was claimed.
    assert_eq!(
        summary.proposals_claimed, 1,
        "exactly the high-risk memory_op proposal should be claimed; got {summary:?}"
    );
    assert_eq!(summary.proposals_completed, 1, "got {summary:?}");
    assert_eq!(summary.proposals_failed, 0, "got {summary:?}");
    assert_eq!(summary.errors, 0, "got {summary:?}");

    // Target proposal: ShadowDone with all v0.3 columns populated.
    let after_target = repo.get(&target_id).await.unwrap();
    assert_eq!(after_target.status, EvolutionStatus::ShadowDone);

    let row: (Option<String>, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT eval_run_id, baseline_metrics_json, shadow_metrics
           FROM evolution_proposals WHERE id = ?",
    )
    .bind(target_id.as_str())
    .fetch_one(store.pool())
    .await
    .unwrap();
    let eval_run_id = row.0.expect("eval_run_id populated");
    assert!(
        eval_run_id.starts_with("eval-"),
        "eval_run_id should follow eval-<...> shape, got {eval_run_id:?}",
    );
    let baseline: Value =
        serde_json::from_str(&row.1.expect("baseline_metrics_json populated")).unwrap();
    let shadow: Value = serde_json::from_str(&row.2.expect("shadow_metrics populated")).unwrap();
    for key in ["eval_run_id", "kind", "total_cases", "pass_rate", "p95_latency_ms"] {
        assert!(
            baseline.get(key).is_some(),
            "baseline missing {key}: {baseline}"
        );
        assert!(
            shadow.get(key).is_some(),
            "shadow missing {key}: {shadow}"
        );
    }
    assert_eq!(shadow["kind"].as_str().unwrap(), "memory_op");
    // Don't pin to total_cases == 1 hard — the rationale per spec is to
    // avoid coupling to fixture count drift. pass_rate >= 0.75 means at
    // least the inline merge case behaved as expected.
    let pass_rate = shadow["pass_rate"].as_f64().unwrap();
    assert!(
        pass_rate >= 0.75,
        "shadow pass_rate must be >= 0.75 across the eval set; got {pass_rate} (shadow={shadow})"
    );

    // Controls: untouched.
    let after_low = repo.get(&low_id).await.unwrap();
    assert_eq!(
        after_low.status,
        EvolutionStatus::Pending,
        "low-risk memory_op must not be claimed",
    );
    let after_skill = repo.get(&skill_id).await.unwrap();
    assert_eq!(
        after_skill.status,
        EvolutionStatus::Pending,
        "skill_update without registered simulator must stay Pending",
    );
}

// ---------------------------------------------------------------------------
// Test B — re-running the loop is a no-op once the proposal is ShadowDone.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn shadow_loop_idempotent_across_runs() {
    let tmp = TempDir::new().unwrap();
    let (store, repo) = fresh_store(&tmp).await;

    let target_id = ProposalId::new("evol-shadow-loop-B-idem");
    repo.insert(&pending_proposal(
        target_id.as_str(),
        EvolutionKind::MemoryOp,
        EvolutionRisk::High,
    ))
    .await
    .unwrap();

    let eval_dir = tmp.path().join("eval");
    write_inline_eval_set(&eval_dir).await;

    let mut runner =
        ShadowRunner::new(repo.clone(), tmp.path().join("kb-missing.sqlite"), eval_dir);
    runner.register_simulator(Arc::new(MemoryOpSimulator) as Arc<dyn KindSimulator>);

    // First pass: claims the proposal, transitions to ShadowDone.
    let first = runner.run_once().await;
    assert_eq!(first.proposals_claimed, 1, "first run should claim 1");
    assert_eq!(first.proposals_completed, 1);

    let after_first = repo.get(&target_id).await.unwrap();
    assert_eq!(after_first.status, EvolutionStatus::ShadowDone);

    // Snapshot first-run shadow blobs so we can prove they're stable.
    let row1: (Option<String>, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT eval_run_id, baseline_metrics_json, shadow_metrics
           FROM evolution_proposals WHERE id = ?",
    )
    .bind(target_id.as_str())
    .fetch_one(store.pool())
    .await
    .unwrap();

    // Second pass: nothing in Pending matching the kind+risk filter, so
    // the runner claims zero proposals and writes nothing.
    let second = runner.run_once().await;
    assert_eq!(
        second.proposals_claimed, 0,
        "second run must claim 0 — the ShadowDone row is no longer Pending; got {second:?}"
    );
    assert_eq!(second.proposals_completed, 0);
    assert_eq!(second.errors, 0);

    let after_second = repo.get(&target_id).await.unwrap();
    assert_eq!(after_second.status, EvolutionStatus::ShadowDone);

    // Critical idempotency contract: shadow_metrics + baseline +
    // eval_run_id from the first run survive the second run unchanged.
    let row2: (Option<String>, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT eval_run_id, baseline_metrics_json, shadow_metrics
           FROM evolution_proposals WHERE id = ?",
    )
    .bind(target_id.as_str())
    .fetch_one(store.pool())
    .await
    .unwrap();
    assert_eq!(row1.0, row2.0, "eval_run_id must not change on re-run");
    assert_eq!(row1.1, row2.1, "baseline_metrics_json must not change");
    assert_eq!(row1.2, row2.2, "shadow_metrics must not change");
}

// ---------------------------------------------------------------------------
// Test C — missing eval-set surfaces the no-eval-set marker, not a stuck row.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn shadow_loop_handles_no_eval_set_dir() {
    let tmp = TempDir::new().unwrap();
    let (store, repo) = fresh_store(&tmp).await;

    let target_id = ProposalId::new("evol-shadow-loop-C-noeval");
    repo.insert(&pending_proposal(
        target_id.as_str(),
        EvolutionKind::MemoryOp,
        EvolutionRisk::High,
    ))
    .await
    .unwrap();

    // Intentionally bogus path: nothing under it, never created.
    let bogus_eval_dir = tmp.path().join("does-not-exist").join("eval");

    let mut runner = ShadowRunner::new(
        repo.clone(),
        tmp.path().join("kb-missing.sqlite"),
        bogus_eval_dir,
    );
    runner.register_simulator(Arc::new(MemoryOpSimulator) as Arc<dyn KindSimulator>);

    let summary = runner.run_once().await;
    // The runner still claims and completes the proposal — the
    // operator-visible signal is `eval_run_id == "no-eval-set"` plus
    // `total_cases == 0`, not a stuck shadow_running row.
    assert_eq!(summary.proposals_claimed, 1, "got {summary:?}");
    assert_eq!(summary.proposals_completed, 1, "got {summary:?}");
    assert_eq!(summary.cases_run, 0, "no cases ran; got {summary:?}");

    let after = repo.get(&target_id).await.unwrap();
    assert_eq!(
        after.status,
        EvolutionStatus::ShadowDone,
        "missing eval set must terminate the proposal, not leave it in ShadowRunning",
    );

    let row: (Option<String>, Option<String>) = sqlx::query_as(
        "SELECT eval_run_id, shadow_metrics FROM evolution_proposals WHERE id = ?",
    )
    .bind(target_id.as_str())
    .fetch_one(store.pool())
    .await
    .unwrap();
    assert_eq!(
        row.0.as_deref(),
        Some("no-eval-set"),
        "operator-visible marker for the untested path",
    );
    let shadow: Value = serde_json::from_str(&row.1.expect("shadow_metrics populated")).unwrap();
    assert_eq!(
        shadow["total_cases"].as_u64().unwrap(),
        0,
        "no-eval-set path must record zero cases",
    );
}
