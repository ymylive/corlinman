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
use corlinman_gateway::routes::admin::{evolution as evolution_routes, AdminState};
use corlinman_plugins::registry::PluginRegistry;
use tempfile::TempDir;
use tower::ServiceExt;

#[tokio::test]
async fn happy_path_list_approve_apply() {
    // 1. Boot a real EvolutionStore over a tempfile and seed one pending
    //    proposal directly through ProposalsRepo so the admin API has
    //    something to find.
    let tmp = TempDir::new().unwrap();
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
        target: "merge_chunks:7,8".into(),
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

    // 2. Stand up the admin sub-router with a minimal AdminState — the
    //    only field the evolution routes consult is `evolution_store`.
    //    The guard layer (Basic auth / cookies) lives one level up in
    //    `router_with_state`; here we directly mount the sub-router so
    //    the test stays focussed on state-machine behaviour.
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(store.clone());
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

    // 5. POST /admin/evolution/:id/apply moves approved → applied.
    //    Phase 2 stub — the response body carries the warning string so
    //    the UI can render it without sniffing for missing fields.
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
    assert_eq!(v["status"], "applied_stub");
    assert_eq!(v["warning"], "real applier not implemented");

    let row = repo.get(&pid).await.unwrap();
    assert_eq!(row.status, EvolutionStatus::Applied);
    assert!(row.applied_at.is_some());
}
