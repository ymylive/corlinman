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
    iso_week_window, EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus,
    EvolutionStore, ProposalId, ProposalsRepo,
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
        eval_run_id: None,
        baseline_metrics_json: None,
        auto_rollback_at: None,
        auto_rollback_reason: None,
    })
    .await
    .unwrap();

    // 2. Stand up the admin sub-router with both an `EvolutionStore`
    //    (read path) and an `EvolutionApplier` (write path). Wave 2-A
    //    swap: the apply route needs the applier; without it the route
    //    returns 503 alongside the rest of the evolution surface.
    let applier = Arc::new(EvolutionApplier::new(
        store.clone(),
        kb.clone(),
        corlinman_core::config::AutoRollbackThresholds::default(),
    ));
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

// ---------------------------------------------------------------------------
// Wave 1-C: budget snapshot endpoint.
// ---------------------------------------------------------------------------

/// Helper: stand up the admin sub-router with just an `EvolutionStore`
/// (no applier needed for the budget endpoint).
async fn budget_app() -> (TempDir, Arc<EvolutionStore>, ProposalsRepo, axum::Router) {
    let tmp = TempDir::new().unwrap();
    let store = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    let repo = ProposalsRepo::new(store.pool().clone());
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(store.clone());
    let app = evolution_routes::router(state);
    (tmp, store, repo, app)
}

async fn read_json(resp: axum::http::Response<Body>) -> serde_json::Value {
    serde_json::from_slice(&to_bytes(resp.into_body(), usize::MAX).await.unwrap()).unwrap()
}

#[tokio::test]
async fn budget_endpoint_returns_zero_when_disabled() {
    // Default `Config` ships `[evolution.budget].enabled = false`, but
    // the route must still respond — the UI gauge wants to render the
    // configured limits even before the operator opts in.
    let (_tmp, _store, _repo, app) = budget_app().await;
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/evolution/budget")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = read_json(resp).await;
    assert_eq!(v["enabled"], false);
    assert_eq!(v["weekly_total"]["limit"], 15);
    assert_eq!(v["weekly_total"]["used"], 0);
    assert_eq!(v["weekly_total"]["remaining"], 15);
    let arr = v["per_kind"].as_array().unwrap();
    // Defaults populate all 8 kinds with non-zero limits.
    assert_eq!(arr.len(), 8);
    // Sorted alphabetically: agent_card → ... → tool_policy.
    assert_eq!(arr[0]["kind"], "agent_card");
    for row in arr {
        assert_eq!(row["used"], 0, "no proposals filed yet → all used = 0");
    }
    // window_*_ms framed by iso_week_window of *some* recent now —
    // exact value depends on wallclock, but must be a 7-day span.
    let start = v["window_start_ms"].as_i64().unwrap();
    let end = v["window_end_ms"].as_i64().unwrap();
    assert_eq!(end - start, 7 * 24 * 3_600 * 1_000);
}

#[tokio::test]
async fn budget_endpoint_reflects_filed_proposals() {
    // Seed 3 memory_op + 1 skill_update inside the current ISO week
    // and confirm both the weekly_total tally and the per_kind row
    // counts move.
    let (_tmp, _store, repo, app) = budget_app().await;

    // Use a `created_at` pinned to the current week so the helper's
    // window query catches it regardless of when the test runs.
    let now_ms = now_ms_local();
    let (start_ms, _end_ms) = iso_week_window(now_ms);
    let in_window = start_ms + 60_000;

    for (i, kind) in [
        EvolutionKind::MemoryOp,
        EvolutionKind::MemoryOp,
        EvolutionKind::MemoryOp,
        EvolutionKind::SkillUpdate,
    ]
    .iter()
    .enumerate()
    {
        repo.insert(&EvolutionProposal {
            id: ProposalId::new(format!("evol-budget-{i}")),
            kind: *kind,
            target: format!("target-{i}"),
            diff: String::new(),
            reasoning: "fixture".into(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Pending,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: in_window + i as i64,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
        })
        .await
        .unwrap();
    }

    let resp = app
        .oneshot(
            Request::builder()
                .uri("/admin/evolution/budget")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v = read_json(resp).await;
    assert_eq!(v["weekly_total"]["used"], 4);
    assert_eq!(v["weekly_total"]["remaining"], 15 - 4);

    let arr = v["per_kind"].as_array().unwrap();
    let memory_row = arr
        .iter()
        .find(|r| r["kind"] == "memory_op")
        .expect("memory_op row");
    assert_eq!(memory_row["limit"], 5);
    assert_eq!(memory_row["used"], 3);
    assert_eq!(memory_row["remaining"], 2);
    let skill_row = arr
        .iter()
        .find(|r| r["kind"] == "skill_update")
        .expect("skill_update row");
    assert_eq!(skill_row["limit"], 3);
    assert_eq!(skill_row["used"], 1);
    assert_eq!(skill_row["remaining"], 2);
}

/// Local copy of the gateway's private `now_ms` helper. Same one-liner
/// the production handler uses; duplicated here so the integration test
/// doesn't have to expose internals.
fn now_ms_local() -> i64 {
    let nanos = time::OffsetDateTime::now_utc().unix_timestamp_nanos();
    (nanos / 1_000_000) as i64
}
