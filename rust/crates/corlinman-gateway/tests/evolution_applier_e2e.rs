//! Wave 2-A end-to-end: with both the `EvolutionStore` and the kb
//! `SqliteStore` plumbed onto `AdminState`, `POST /admin/evolution/:id/apply`
//! should run the real applier — mutate `kb.sqlite`, write an
//! `evolution_history` row, flip the proposal status — instead of the
//! Phase 2 stub.
//!
//! No HTTP server is bound — `tower::ServiceExt::oneshot` calls the axum
//! `Router` in-process. The point is to prove the wiring between the
//! admin route, the `EvolutionApplier`, the kb store, and the evolution
//! store stays consistent end to end.

use std::sync::Arc;

use arc_swap::ArcSwap;
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use corlinman_core::config::Config;
use corlinman_evolution::{
    EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore, ProposalId,
    ProposalsRepo,
};
use corlinman_gateway::evolution_applier::EvolutionApplier;
use corlinman_gateway::routes::admin::{evolution as evolution_routes, AdminState};
use corlinman_plugins::registry::PluginRegistry;
use corlinman_vector::SqliteStore;
use tempfile::TempDir;
use tower::ServiceExt;

/// Boot kb + evolution stores, build the admin sub-router with a real
/// `EvolutionApplier` attached, and seed an approved `merge_chunks`
/// proposal pointing at two real kb chunks. Then exercise `/apply` and
/// assert kb + history + proposal all moved.
#[tokio::test]
async fn apply_runs_real_merge_chunks_pipeline() {
    let tmp = TempDir::new().unwrap();

    // 1. Open kb.sqlite + seed two chunks for the merge target.
    let kb = Arc::new(
        SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap(),
    );
    let file_id = kb
        .insert_file("/notes.md", "diary", "checksum", 0, 64)
        .await
        .unwrap();
    let winner_id = kb
        .insert_chunk(file_id, 0, "winner content", None, "general")
        .await
        .unwrap();
    let loser_id = kb
        .insert_chunk(file_id, 1, "loser content", None, "general")
        .await
        .unwrap();

    // 2. Open evolution.sqlite + seed an approved memory_op proposal
    //    targeting the two chunks.
    let evol = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    let repo = ProposalsRepo::new(evol.pool().clone());
    let pid = ProposalId::new("evol-e2e-w2a-001");
    let target = format!("merge_chunks:{winner_id},{loser_id}");
    repo.insert(&EvolutionProposal {
        id: pid.clone(),
        kind: EvolutionKind::MemoryOp,
        target: target.clone(),
        diff: String::new(),
        reasoning: "duplicate context".into(),
        risk: EvolutionRisk::Low,
        budget_cost: 0,
        status: EvolutionStatus::Approved,
        shadow_metrics: None,
        signal_ids: vec![],
        trace_ids: vec![],
        created_at: 1_000,
        decided_at: Some(2_000),
        decided_by: Some("e2e-operator".into()),
        applied_at: None,
        rollback_of: None,
        eval_run_id: None,
        baseline_metrics_json: None,
        auto_rollback_at: None,
        auto_rollback_reason: None,
    })
    .await
    .unwrap();

    // 3. Build the applier + admin sub-router.
    let applier = Arc::new(EvolutionApplier::new(
        evol.clone(),
        kb.clone(),
        corlinman_core::config::AutoRollbackThresholds::default(),
    ));
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(evol.clone())
    .with_evolution_applier(applier);
    let app = evolution_routes::router(state);

    // 4. POST /admin/evolution/:id/apply — expect 200 + history_id +
    //    `status: "applied"` (no more `applied_stub`).
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/admin/evolution/{}/apply", pid.as_str()))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: serde_json::Value =
        serde_json::from_slice(&to_bytes(resp.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!(body["status"], "applied");
    let history_id = body["history_id"].as_i64().expect("history_id present");
    assert!(history_id > 0);

    // 5. kb.sqlite: loser deleted, winner kept.
    let rows = kb
        .query_chunks_by_ids(&[winner_id, loser_id])
        .await
        .unwrap();
    assert_eq!(rows.len(), 1, "loser should be deleted");
    assert_eq!(rows[0].id, winner_id);

    // 6. evolution.sqlite: proposal status flipped, history row exists
    //    with the same id we got back.
    let after = repo.get(&pid).await.unwrap();
    assert_eq!(after.status, EvolutionStatus::Applied);
    assert!(after.applied_at.is_some());

    let hist: (i64, String, String, String) = sqlx::query_as(
        "SELECT id, proposal_id, target, inverse_diff
           FROM evolution_history WHERE id = ?",
    )
    .bind(history_id)
    .fetch_one(evol.pool())
    .await
    .unwrap();
    assert_eq!(hist.0, history_id);
    assert_eq!(hist.1, pid.as_str());
    assert_eq!(hist.2, target);
    let inverse: serde_json::Value = serde_json::from_str(&hist.3).unwrap();
    assert_eq!(inverse["action"], "restore_chunk");
    assert_eq!(inverse["loser_id"], loser_id);
    assert_eq!(inverse["loser_content"], "loser content");
}

/// `delete_chunk:<id>` over the same router: the kb row vanishes and
/// the inverse diff captures enough to reconstruct it.
#[tokio::test]
async fn apply_runs_real_delete_chunk_pipeline() {
    let tmp = TempDir::new().unwrap();
    let kb = Arc::new(
        SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap(),
    );
    let file_id = kb
        .insert_file("/d.md", "diary", "checksum", 0, 32)
        .await
        .unwrap();
    let chunk_id = kb
        .insert_chunk(file_id, 0, "doomed", None, "general")
        .await
        .unwrap();

    let evol = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    let repo = ProposalsRepo::new(evol.pool().clone());
    let pid = ProposalId::new("evol-e2e-w2a-002");
    let target = format!("delete_chunk:{chunk_id}");
    repo.insert(&EvolutionProposal {
        id: pid.clone(),
        kind: EvolutionKind::MemoryOp,
        target: target.clone(),
        diff: String::new(),
        reasoning: String::new(),
        risk: EvolutionRisk::Low,
        budget_cost: 0,
        status: EvolutionStatus::Approved,
        shadow_metrics: None,
        signal_ids: vec![],
        trace_ids: vec![],
        created_at: 1_000,
        decided_at: Some(2_000),
        decided_by: Some("op".into()),
        applied_at: None,
        rollback_of: None,
        eval_run_id: None,
        baseline_metrics_json: None,
        auto_rollback_at: None,
        auto_rollback_reason: None,
    })
    .await
    .unwrap();

    let applier = Arc::new(EvolutionApplier::new(
        evol.clone(),
        kb.clone(),
        corlinman_core::config::AutoRollbackThresholds::default(),
    ));
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(evol.clone())
    .with_evolution_applier(applier);
    let app = evolution_routes::router(state);

    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/admin/evolution/{}/apply", pid.as_str()))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let rows = kb.query_chunks_by_ids(&[chunk_id]).await.unwrap();
    assert!(rows.is_empty(), "chunk row deleted");
    let after = repo.get(&pid).await.unwrap();
    assert_eq!(after.status, EvolutionStatus::Applied);
}
