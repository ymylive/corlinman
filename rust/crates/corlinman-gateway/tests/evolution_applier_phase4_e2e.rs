//! Phase 4 W1 4-1D end-to-end: with the gateway's admin sub-router
//! mounted on a real `EvolutionApplier`, `POST /admin/evolution/:id/apply`
//! and `POST /admin/evolution/:id/rollback` exercise the new
//! `prompt_template` and `tool_policy` kinds against on-disk per-tenant
//! state.
//!
//! The tests don't bind an HTTP listener; they call the axum `Router`
//! in-process via `tower::ServiceExt::oneshot`. The point is to prove
//! the wiring between the admin route, the applier, the per-tenant
//! files, and the evolution audit row stays consistent end to end.

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
use serde_json::json;
use tempfile::TempDir;
use tower::ServiceExt;

/// Bring up a fresh applier + admin sub-router. Returns the tempdir
/// (kept alive by the caller for FS assertions), the applier handle
/// (so revert tests can drive `EvolutionApplier::revert` directly —
/// the admin sub-router only exposes `/apply`; rollback is invoked
/// programmatically by the AutoRollback monitor), the evolution
/// store, and the axum router pre-wired onto the admin state.
async fn boot() -> (
    TempDir,
    Arc<EvolutionApplier>,
    Arc<EvolutionStore>,
    axum::Router,
) {
    let tmp = TempDir::new().unwrap();
    // Phase 3.1 / S-5 sandbox discipline: all on-disk writes resolve
    // under `<tempdir>/tenants/...`. The applier derives that root by
    // walking up from `skills_dir`, so we must construct
    // `skills_dir = <tempdir>/skills` for the parent to exist.
    let kb = Arc::new(
        SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap(),
    );
    let evol = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    let skills_dir = tmp.path().join("skills");
    std::fs::create_dir_all(&skills_dir).unwrap();

    let applier = Arc::new(EvolutionApplier::new(
        evol.clone(),
        kb,
        corlinman_core::config::AutoRollbackThresholds::default(),
        skills_dir,
    ));
    let state = AdminState::new(
        Arc::new(PluginRegistry::default()),
        Arc::new(ArcSwap::from_pointee(Config::default())),
    )
    .with_evolution_store(evol.clone())
    .with_evolution_applier(applier.clone());
    (tmp, applier, evol, evolution_routes::router(state))
}

/// Insert an Approved proposal of the given kind. Mirrors the helper
/// pattern in the unit tests but lives in this file because the e2e
/// flow boots its own state.
async fn seed_approved(
    evol: &EvolutionStore,
    id: &str,
    kind: EvolutionKind,
    target: &str,
    diff: &str,
) -> ProposalId {
    let pid = ProposalId::new(id);
    let repo = ProposalsRepo::new(evol.pool().clone());
    repo.insert(&EvolutionProposal {
        id: pid.clone(),
        kind,
        target: target.into(),
        diff: diff.into(),
        reasoning: "phase 4 e2e".into(),
        risk: EvolutionRisk::High,
        budget_cost: 3,
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
    pid
}

async fn post(app: axum::Router, uri: &str) -> (StatusCode, serde_json::Value) {
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let body: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
    };
    (status, body)
}

/// Apply a `prompt_template` proposal end-to-end and verify the
/// per-tenant segment file lands on disk + the evolution audit row
/// captures the inverse_diff for revert.
#[tokio::test]
async fn apply_prompt_template_writes_segment_file() {
    let (tmp, _applier, evol, app) = boot().await;
    let pid = seed_approved(
        &evol,
        "evol-pt-e2e-001",
        EvolutionKind::PromptTemplate,
        "agent.greeting",
        &json!({
            "before": "",
            "after": "Hello, friend.",
            "rationale": "warmer welcome",
        })
        .to_string(),
    )
    .await;

    let (status, body) = post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert_eq!(status, StatusCode::OK, "apply returned {body}");
    assert_eq!(body["status"], "applied");

    // Default tenant — file lives under `<tmp>/tenants/default/...`.
    let segment_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("prompt_segments")
        .join("agent.greeting.md");
    assert!(segment_path.exists(), "segment file written");
    assert_eq!(
        std::fs::read_to_string(&segment_path).unwrap(),
        "Hello, friend.",
    );

    // Audit row carries the inverse_diff.
    let row: (String, String) = sqlx::query_as(
        "SELECT target, inverse_diff FROM evolution_history WHERE proposal_id = ?",
    )
    .bind(pid.as_str())
    .fetch_one(evol.pool())
    .await
    .unwrap();
    assert_eq!(row.0, "agent.greeting");
    let inv: serde_json::Value = serde_json::from_str(&row.1).unwrap();
    assert_eq!(inv["op"], "prompt_template");
    assert_eq!(inv["tenant"], "default");
    assert_eq!(inv["before_present"], false);
}

/// Tenant-prefixed targets (`acme::agent.greeting`) route to the
/// named tenant's directory. Verifies multi-tenant fan-out from a
/// single applier instance.
#[tokio::test]
async fn apply_prompt_template_routes_named_tenant() {
    let (tmp, _applier, evol, app) = boot().await;
    let pid = seed_approved(
        &evol,
        "evol-pt-e2e-tenant-001",
        EvolutionKind::PromptTemplate,
        "acme::agent.greeting",
        &json!({
            "before": "",
            "after": "Welcome to ACME.",
            "rationale": "branded greeting",
        })
        .to_string(),
    )
    .await;
    let (status, _body) = post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert_eq!(status, StatusCode::OK);

    let acme_path = tmp
        .path()
        .join("tenants")
        .join("acme")
        .join("prompt_segments")
        .join("agent.greeting.md");
    let default_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("prompt_segments")
        .join("agent.greeting.md");
    assert_eq!(
        std::fs::read_to_string(&acme_path).unwrap(),
        "Welcome to ACME."
    );
    assert!(
        !default_path.exists(),
        "default tenant must not be touched when target prefix names another tenant"
    );
}

/// Apply a `tool_policy` proposal and confirm the toml file flips
/// the targeted mode while leaving sibling tools intact.
#[tokio::test]
async fn apply_tool_policy_flips_mode_in_toml() {
    let (tmp, _applier, evol, app) = boot().await;

    // Seed prior state with the matching `before` mode + a sibling
    // tool that must survive the apply.
    let toml_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("tool_policy.toml");
    std::fs::create_dir_all(toml_path.parent().unwrap()).unwrap();
    std::fs::write(
        &toml_path,
        "[web_search]\nmode = \"auto\"\nrule_id = \"baseline\"\n[other_tool]\nmode = \"prompt\"\nrule_id = \"keep\"\n",
    )
    .unwrap();

    let pid = seed_approved(
        &evol,
        "evol-tp-e2e-001",
        EvolutionKind::ToolPolicy,
        "web_search",
        &json!({
            "before": "auto",
            "after": "deny",
            "rule_id": "rule-quarantine",
        })
        .to_string(),
    )
    .await;

    let (status, body) = post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert_eq!(status, StatusCode::OK, "apply returned {body}");
    assert_eq!(body["status"], "applied");

    let parsed: toml::Table = std::fs::read_to_string(&toml_path)
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(parsed["web_search"]["mode"].as_str(), Some("deny"));
    assert_eq!(parsed["web_search"]["rule_id"].as_str(), Some("rule-quarantine"));
    assert_eq!(
        parsed["other_tool"]["mode"].as_str(),
        Some("prompt"),
        "sibling tool kept across apply"
    );
}

/// `tool_policy` apply against drifted state must surface a 4xx (no
/// FS write happens) and the toml is left exactly as the operator
/// last wrote it.
#[tokio::test]
async fn apply_tool_policy_drift_mismatch_returns_4xx() {
    let (tmp, _applier, evol, app) = boot().await;
    let toml_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("tool_policy.toml");
    std::fs::create_dir_all(toml_path.parent().unwrap()).unwrap();
    std::fs::write(
        &toml_path,
        "[web_search]\nmode = \"prompt\"\nrule_id = \"manual\"\n",
    )
    .unwrap();
    let baseline = std::fs::read_to_string(&toml_path).unwrap();

    let pid = seed_approved(
        &evol,
        "evol-tp-e2e-drift-001",
        EvolutionKind::ToolPolicy,
        "web_search",
        &json!({
            "before": "auto", // drift: disk says prompt
            "after": "deny",
            "rule_id": "rule-x",
        })
        .to_string(),
    )
    .await;

    let (status, _body) = post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert!(
        status.is_client_error() || status.is_server_error(),
        "drift must not return 200; got {status}"
    );
    // toml file untouched.
    assert_eq!(std::fs::read_to_string(&toml_path).unwrap(), baseline);
}

/// Full apply → rollback round-trip for `tool_policy`. The admin
/// sub-router only mounts `/apply`; the rollback path is invoked
/// programmatically (the AutoRollback monitor calls
/// `EvolutionApplier::revert` directly), so we drive that side via
/// the applier handle instead of an HTTP round-trip. Pins the
/// inverse_diff round-trip and the on-disk restore.
#[tokio::test]
async fn rollback_tool_policy_restores_prior_mode() {
    let (tmp, applier, evol, app) = boot().await;
    let toml_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("tool_policy.toml");
    std::fs::create_dir_all(toml_path.parent().unwrap()).unwrap();
    std::fs::write(
        &toml_path,
        "[web_search]\nmode = \"auto\"\nrule_id = \"original\"\n",
    )
    .unwrap();

    let pid = seed_approved(
        &evol,
        "evol-tp-e2e-rollback-001",
        EvolutionKind::ToolPolicy,
        "web_search",
        &json!({
            "before": "auto",
            "after": "deny",
            "rule_id": "rule-bad",
        })
        .to_string(),
    )
    .await;

    let (status, _body) =
        post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert_eq!(status, StatusCode::OK);
    let parsed: toml::Table = std::fs::read_to_string(&toml_path)
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(parsed["web_search"]["mode"].as_str(), Some("deny"));

    // Rollback path: drive the applier directly. Same code path the
    // AutoRollback monitor uses in production — the admin route is
    // intentionally not the rollback entry point.
    applier
        .revert(&pid, "metrics regression")
        .await
        .expect("revert succeeds");

    let parsed: toml::Table = std::fs::read_to_string(&toml_path)
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        parsed["web_search"]["mode"].as_str(),
        Some("auto"),
        "rollback restores pre-apply mode"
    );
}

/// `prompt_template` apply → rollback round-trip. The forward apply
/// creates a brand-new segment file (before_present=false on disk);
/// the revert removes it. As with the tool_policy round-trip above,
/// rollback is driven via the applier handle — the admin sub-router
/// owns `/apply` only.
#[tokio::test]
async fn rollback_prompt_template_removes_created_segment() {
    let (tmp, applier, evol, app) = boot().await;
    let pid = seed_approved(
        &evol,
        "evol-pt-e2e-rollback-001",
        EvolutionKind::PromptTemplate,
        "agent.greeting",
        &json!({
            "before": "",
            "after": "first take",
            "rationale": "v1",
        })
        .to_string(),
    )
    .await;

    let (status, _body) =
        post(app, &format!("/admin/evolution/{}/apply", pid.as_str())).await;
    assert_eq!(status, StatusCode::OK);
    let segment_path = tmp
        .path()
        .join("tenants")
        .join("default")
        .join("prompt_segments")
        .join("agent.greeting.md");
    assert!(segment_path.exists());

    applier
        .revert(&pid, "rollback the segment")
        .await
        .expect("revert succeeds");
    assert!(
        !segment_path.exists(),
        "rollback must remove the segment file when before_present=false"
    );
}
