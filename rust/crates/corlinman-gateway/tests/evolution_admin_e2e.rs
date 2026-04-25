//! Wave 1-C end-to-end: with an `EvolutionStore` plumbed onto `AdminState`,
//! the `/admin/evolution/*` admin router should walk a fake proposal
//! through the happy-path state machine.
//!
//! We seed `evolution_proposals` directly via `ProposalsRepo::insert`,
//! then exercise the same router `routes::admin::evolution::router` returns.
//! No HTTP server is bound — `tower::ServiceExt::oneshot` calls the axum
//! `Router` in-process. The point is not to exercise the network, it's to
//! prove the wire-up between `EvolutionStore`, `AdminState`, the sub-router,
//! and the typed `ProposalsRepo` is consistent end to end.

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

#[tokio::test]
async fn happy_path_list_approve_apply() {
    // 1. Boot real EvolutionStore + kb SqliteStore over a tempdir.
    //    Seed two real chunks so the wave 2-A applier has something to
    //    merge when we hit /apply at the end.
    let tmp = TempDir::new().unwrap();
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

    let store = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    let repo = ProposalsRepo::new(store.pool().clone());
    let pid = ProposalId::new("evol-e2e-001");
    repo.insert(&EvolutionProposal {
        id: pid.clone(),
        kind: EvolutionKind::MemoryOp,
        target: format!("merge_chunks:{winner_id},{loser_id}"),
        diff: String::new(),
        reasoning: "duplicate context".into(),
        risk: EvolutionRisk::Low,
        budget_cost: 0,
        status: EvolutionStatus::Pending,
        shadow_metrics: None,
        signal_ids: vec![10, 11],
        trace_ids: vec!["t-e2e".into()],
        created_at: 1_000,
        decided_at: None,
        decided_by: None,
        applied_at: None,
        rollback_of: None,
    })
    .await
    .unwrap();

    // 2. Stand up the admin sub-router with both an `EvolutionStore`
    //    (read path) and an `EvolutionApplier` (write path). Wave 2-A
    //    swap: the apply route needs the applier; without it the route
    //    returns 503 alongside the rest of the evolution surface.
    let applier = Arc::new(EvolutionApplier::new(store.clone(), kb.clone()));
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(store.clone())
    .with_evolution_applier(applier);
    let app = evolution_routes::router(state);

    // 3. GET /admin/evolution should return our pending proposal.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/admin/evolution")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v: serde_json::Value =
        serde_json::from_slice(&to_bytes(resp.into_body(), usize::MAX).await.unwrap()).unwrap();
    let arr = v.as_array().unwrap();
    assert_eq!(arr.len(), 1, "expected one pending proposal");
    assert_eq!(arr[0]["id"], "evol-e2e-001");
    assert_eq!(arr[0]["status"], "pending");

    // 4. POST /admin/evolution/:id/approve flips it to approved.
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/evolution/evol-e2e-001/approve")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"decided_by":"e2e-operator"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let row = repo.get(&pid).await.unwrap();
    assert_eq!(row.status, EvolutionStatus::Approved);
    assert_eq!(row.decided_by.as_deref(), Some("e2e-operator"));
    assert!(row.decided_at.is_some());

    // 5. POST /admin/evolution/:id/apply moves approved → applied via
    //    the wave 2-A real applier — kb.sqlite mutates, an
    //    `evolution_history` row lands, the proposal flips. The
    //    response carries the new `history_id` field so the UI can
    //    deep-link into the audit trail.
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/admin/evolution/evol-e2e-001/apply")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v: serde_json::Value =
        serde_json::from_slice(&to_bytes(resp.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!(v["status"], "applied");
    assert!(v["history_id"].is_i64());

    let row = repo.get(&pid).await.unwrap();
    assert_eq!(row.status, EvolutionStatus::Applied);
    assert!(row.applied_at.is_some());

    // kb side: loser deleted, winner still present.
    let kb_rows = kb.query_chunks_by_ids(&[winner_id, loser_id]).await.unwrap();
    assert_eq!(kb_rows.len(), 1);
    assert_eq!(kb_rows[0].id, winner_id);
}
