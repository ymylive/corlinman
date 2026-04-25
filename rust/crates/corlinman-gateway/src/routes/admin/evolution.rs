//! `/admin/evolution*` — proposal queue admin endpoints (Wave 1-C).
//!
//! Five routes live here, all behind the same Basic-auth / cookie guard
//! the rest of the admin surface sits behind:
//!
//! - `GET  /admin/evolution`               — list proposals filtered by
//!   `?status=pending&limit=50` (defaults: `pending`, 50, max 200).
//! - `GET  /admin/evolution/:id`           — single proposal detail.
//! - `POST /admin/evolution/:id/approve`   — body `{"decided_by": "..."}`.
//!   Transitions `pending|shadow_done → approved` + writes
//!   `decided_at` / `decided_by`.
//! - `POST /admin/evolution/:id/deny`      — body
//!   `{"decided_by": "...", "reason": "..."}`. Transitions
//!   `pending|shadow_done → denied`. The reason (when supplied) is appended
//!   to `reasoning` with a `[DENIED: ...]` prefix so the audit trail keeps
//!   it.
//! - `POST /admin/evolution/:id/apply`     — Phase 2 stub. Transitions
//!   `approved → applied` and stamps `applied_at`. The real
//!   `EvolutionApplier` lands in Phase 3 — until then the response carries
//!   `{"status": "applied_stub", "warning": "real applier not implemented"}`
//!   so callers don't mistake the stub for the real thing.
//!
//! ### State machine
//!
//! Illegal transitions — `apply` on a `pending` row, `approve` on an
//! already-applied row, etc — return **409 Conflict** with
//! `{"error": "invalid_state_transition", "from": "...", "to": "..."}`.
//! Allowed transitions are:
//!
//! ```text
//! pending ─┐
//!          ├─► approved ──► applied
//! shadow_done ─┘   │
//!                  └─► denied
//! ```
//!
//! ### Disabled mode
//!
//! When `[evolution.observer.enabled] = false` (or the SQLite open at boot
//! failed) the gateway boots without an `evolution_store` attached.
//! Every route here then returns **503 Service Unavailable** with
//! `{"error": "evolution_disabled", ...}`, mirroring the approval-gate
//! convention so the UI can render a single "subsystem off" banner.

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_core::metrics::{
    EVOLUTION_PROPOSALS_APPLIED, EVOLUTION_PROPOSALS_DECISION, EVOLUTION_PROPOSALS_LISTED,
};
use corlinman_evolution::{
    EvolutionProposal, EvolutionStatus, EvolutionStore, ProposalId, ProposalsRepo, RepoError,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sqlx::SqlitePool;
use std::str::FromStr;
use tracing::warn;

use super::AdminState;

/// Default `?limit=` when the caller doesn't pass one. Same ballpark as
/// `/admin/approvals` (no explicit limit there, but the UI batches in 50s).
const DEFAULT_LIMIT: i64 = 50;

/// Hard ceiling on `?limit=` so a buggy client can't pull the whole table.
const MAX_LIMIT: i64 = 200;

/// Sub-router for `/admin/evolution*`. Mounted by
/// [`super::router_with_state`] inside the admin_auth middleware.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/evolution", get(list_proposals))
        .route("/admin/evolution/:id", get(get_proposal))
        .route("/admin/evolution/:id/approve", post(approve_proposal))
        .route("/admin/evolution/:id/deny", post(deny_proposal))
        .route("/admin/evolution/:id/apply", post(apply_proposal))
        .with_state(state)
}

/// Query params for the list endpoint. Both fields default — calling
/// `GET /admin/evolution` with no query string returns up to 50 pending
/// proposals.
#[derive(Debug, Deserialize, Default)]
pub struct ListQuery {
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub limit: Option<i64>,
}

/// Wire shape for the proposal table. `EvolutionProposal` already
/// `Serialize`s to a useful JSON, but rebuilding it here pins the API
/// contract independently of any future internal struct churn.
#[derive(Debug, Serialize)]
pub struct ProposalOut {
    pub id: String,
    pub kind: String,
    pub target: String,
    pub diff: String,
    pub reasoning: String,
    pub risk: String,
    pub budget_cost: u32,
    pub status: String,
    pub shadow_metrics: Option<serde_json::Value>,
    pub signal_ids: Vec<i64>,
    pub trace_ids: Vec<String>,
    pub created_at: i64,
    pub decided_at: Option<i64>,
    pub decided_by: Option<String>,
    pub applied_at: Option<i64>,
    pub rollback_of: Option<String>,
}

impl From<EvolutionProposal> for ProposalOut {
    fn from(p: EvolutionProposal) -> Self {
        Self {
            id: p.id.0,
            kind: p.kind.as_str().to_string(),
            target: p.target,
            diff: p.diff,
            reasoning: p.reasoning,
            risk: p.risk.as_str().to_string(),
            budget_cost: p.budget_cost,
            status: p.status.as_str().to_string(),
            shadow_metrics: p.shadow_metrics.and_then(|m| serde_json::to_value(m).ok()),
            signal_ids: p.signal_ids,
            trace_ids: p.trace_ids,
            created_at: p.created_at,
            decided_at: p.decided_at,
            decided_by: p.decided_by,
            applied_at: p.applied_at,
            rollback_of: p.rollback_of.map(|p| p.0),
        }
    }
}

/// Resolve `(ProposalsRepo, SqlitePool)` from the shared `EvolutionStore`.
/// The pool clone is needed for the one-off `UPDATE evolution_proposals
/// SET reasoning = ?` issued on deny — `ProposalsRepo` (in
/// `corlinman-evolution`) doesn't expose a setter and adding one is out of
/// scope for this task.
fn resolve_handles(store: &EvolutionStore) -> (ProposalsRepo, SqlitePool) {
    let pool = store.pool().clone();
    (ProposalsRepo::new(pool.clone()), pool)
}

fn evolution_disabled() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "evolution_disabled",
            "message": "evolution proposal queue is not configured on this gateway",
        })),
    )
        .into_response()
}

fn invalid_state_transition(from: EvolutionStatus, to: EvolutionStatus) -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "invalid_state_transition",
            "from": from.as_str(),
            "to": to.as_str(),
        })),
    )
        .into_response()
}

fn not_found(id: &str) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({
            "error": "not_found",
            "resource": "evolution_proposal",
            "id": id,
        })),
    )
        .into_response()
}

fn storage_error(err: RepoError, ctx: &'static str) -> Response {
    warn!(error = %err, "admin/evolution {ctx} failed");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

async fn list_proposals(State(state): State<AdminState>, Query(q): Query<ListQuery>) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, _) = resolve_handles(store);
    let status_str = q.status.as_deref().unwrap_or("pending");
    let status = match EvolutionStatus::from_str(status_str) {
        Ok(s) => s,
        Err(err) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "invalid_status",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    };
    let limit = q.limit.unwrap_or(DEFAULT_LIMIT).clamp(1, MAX_LIMIT);

    match repo.list_by_status(status, limit).await {
        Ok(rows) => {
            EVOLUTION_PROPOSALS_LISTED.inc();
            let out: Vec<ProposalOut> = rows.into_iter().map(Into::into).collect();
            Json(out).into_response()
        }
        Err(err) => storage_error(err, "list"),
    }
}

async fn get_proposal(State(state): State<AdminState>, Path(id): Path<String>) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, _) = resolve_handles(store);
    let pid = ProposalId::new(id.clone());
    match repo.get(&pid).await {
        Ok(p) => Json(ProposalOut::from(p)).into_response(),
        Err(RepoError::NotFound(_)) => not_found(&id),
        Err(err) => storage_error(err, "get"),
    }
}

#[derive(Debug, Deserialize)]
pub struct ApproveBody {
    pub decided_by: String,
}

#[derive(Debug, Deserialize)]
pub struct DenyBody {
    pub decided_by: String,
    #[serde(default)]
    pub reason: Option<String>,
}

async fn approve_proposal(
    State(state): State<AdminState>,
    Path(id): Path<String>,
    Json(body): Json<ApproveBody>,
) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, _) = resolve_handles(store);
    let pid = ProposalId::new(id.clone());
    let current = match repo.get(&pid).await {
        Ok(p) => p,
        Err(RepoError::NotFound(_)) => return not_found(&id),
        Err(err) => return storage_error(err, "approve.get"),
    };
    if !can_decide(current.status) {
        return invalid_state_transition(current.status, EvolutionStatus::Approved);
    }
    if let Err(err) = repo
        .set_decision(&pid, EvolutionStatus::Approved, now_ms(), &body.decided_by)
        .await
    {
        return match err {
            RepoError::NotFound(_) => not_found(&id),
            other => storage_error(other, "approve.set_decision"),
        };
    }
    EVOLUTION_PROPOSALS_DECISION
        .with_label_values(&["approved"])
        .inc();
    Json(json!({
        "id": id,
        "status": EvolutionStatus::Approved.as_str(),
    }))
    .into_response()
}

async fn deny_proposal(
    State(state): State<AdminState>,
    Path(id): Path<String>,
    Json(body): Json<DenyBody>,
) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, pool) = resolve_handles(store);
    let pid = ProposalId::new(id.clone());
    let current = match repo.get(&pid).await {
        Ok(p) => p,
        Err(RepoError::NotFound(_)) => return not_found(&id),
        Err(err) => return storage_error(err, "deny.get"),
    };
    if !can_decide(current.status) {
        return invalid_state_transition(current.status, EvolutionStatus::Denied);
    }

    // Preserve the deny reason inside `reasoning` with a fixed prefix so
    // the history endpoint surfaces it without needing a new column.
    // `ProposalsRepo` doesn't expose a setter for `reasoning`; we issue
    // one targeted UPDATE through the same pool and accept the slight
    // duplication (the row write is the only bit done outside the typed
    // API).
    if let Some(reason) = body
        .reason
        .as_ref()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
    {
        let updated = if current.reasoning.is_empty() {
            format!("[DENIED: {reason}]")
        } else {
            format!("{}\n[DENIED: {reason}]", current.reasoning)
        };
        match sqlx::query("UPDATE evolution_proposals SET reasoning = ? WHERE id = ?")
            .bind(&updated)
            .bind(pid.as_str())
            .execute(&pool)
            .await
        {
            Ok(res) if res.rows_affected() == 0 => return not_found(&id),
            Ok(_) => {}
            Err(err) => {
                warn!(error = %err, "admin/evolution deny.update_reasoning failed");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({
                        "error": "storage_error",
                        "message": err.to_string(),
                    })),
                )
                    .into_response();
            }
        }
    }

    if let Err(err) = repo
        .set_decision(&pid, EvolutionStatus::Denied, now_ms(), &body.decided_by)
        .await
    {
        return match err {
            RepoError::NotFound(_) => not_found(&id),
            other => storage_error(other, "deny.set_decision"),
        };
    }
    EVOLUTION_PROPOSALS_DECISION
        .with_label_values(&["denied"])
        .inc();
    Json(json!({
        "id": id,
        "status": EvolutionStatus::Denied.as_str(),
    }))
    .into_response()
}

async fn apply_proposal(State(state): State<AdminState>, Path(id): Path<String>) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, _) = resolve_handles(store);
    let pid = ProposalId::new(id.clone());
    let current = match repo.get(&pid).await {
        Ok(p) => p,
        Err(RepoError::NotFound(_)) => return not_found(&id),
        Err(err) => return storage_error(err, "apply.get"),
    };
    if current.status != EvolutionStatus::Approved {
        return invalid_state_transition(current.status, EvolutionStatus::Applied);
    }
    if let Err(err) = repo.mark_applied(&pid, now_ms()).await {
        return match err {
            RepoError::NotFound(_) => not_found(&id),
            other => storage_error(other, "apply.mark_applied"),
        };
    }
    EVOLUTION_PROPOSALS_APPLIED.inc();
    // Phase 2 stub: the real `EvolutionApplier` lands in Phase 3. The
    // response shape signals to the UI that nothing was actually mutated
    // on disk — only the proposal row's status flipped.
    Json(json!({
        "id": id,
        "status": "applied_stub",
        "warning": "real applier not implemented",
    }))
    .into_response()
}

/// Approve / deny are allowed from `pending` and `shadow_done`. Any other
/// status (already-decided, applied, rolled-back, shadow-running) means the
/// caller raced or the UI is out of sync.
fn can_decide(status: EvolutionStatus) -> bool {
    matches!(
        status,
        EvolutionStatus::Pending | EvolutionStatus::ShadowDone
    )
}

/// Unix milliseconds. Local helper so this module doesn't pull a
/// time-source dependency from `corlinman-evolution` (the crate keeps its
/// own helper private).
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
    use crate::middleware::approval::ApprovalGate;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_core::config::Config;
    use corlinman_evolution::{
        EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore,
        ProposalId,
    };
    use corlinman_plugins::registry::PluginRegistry;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tower::ServiceExt;

    /// Build a fresh `EvolutionStore` + `ProposalsRepo` over a temp file.
    /// Returns both: the repo for direct test setup / assertions, the
    /// store for plugging into `AdminState`.
    async fn fresh_store() -> (TempDir, Arc<EvolutionStore>, ProposalsRepo) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = Arc::new(EvolutionStore::open(&path).await.unwrap());
        let repo = ProposalsRepo::new(store.pool().clone());
        (tmp, store, repo)
    }

    fn proposal(id: &str, status: EvolutionStatus) -> EvolutionProposal {
        EvolutionProposal {
            id: ProposalId::new(id),
            kind: EvolutionKind::MemoryOp,
            target: "merge_chunks:1,2".into(),
            diff: String::new(),
            reasoning: "two duplicate chunks".into(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status,
            shadow_metrics: None,
            signal_ids: vec![1, 2],
            trace_ids: vec!["t1".into()],
            created_at: 1_000,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
        }
    }

    fn app_with(store: Option<Arc<EvolutionStore>>) -> Router {
        let state = AdminState {
            plugins: Arc::new(PluginRegistry::default()),
            config: Arc::new(ArcSwap::from_pointee(Config::default())),
            approval_gate: None as Option<Arc<ApprovalGate>>,
            session_store: None,
            config_path: None,
            log_broadcast: None,
            rag_store: None,
            scheduler_history: None,
            py_config_path: None,
            config_watcher: None,
            evolution_store: store,
        };
        router(state)
    }

    async fn read_json(resp: Response) -> serde_json::Value {
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    #[tokio::test]
    async fn list_returns_503_when_store_missing() {
        let app = app_with(None);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn list_defaults_to_pending() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        repo.insert(&proposal("p2", EvolutionStatus::Approved))
            .await
            .unwrap();

        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        let arr = v.as_array().unwrap();
        assert_eq!(arr.len(), 1, "default filter is status=pending");
        assert_eq!(arr[0]["id"], "p1");
        assert_eq!(arr[0]["status"], "pending");
    }

    #[tokio::test]
    async fn list_clamps_limit_to_max() {
        let (_tmp, store, repo) = fresh_store().await;
        for i in 0..5 {
            repo.insert(&proposal(&format!("p{i}"), EvolutionStatus::Pending))
                .await
                .unwrap();
        }
        let app = app_with(Some(store));
        // limit=999 must clamp to 200 (and we still get all 5).
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution?limit=999")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v.as_array().unwrap().len(), 5);
    }

    #[tokio::test]
    async fn get_returns_proposal_detail() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/p1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v["id"], "p1");
        assert_eq!(v["kind"], "memory_op");
        assert_eq!(v["signal_ids"], serde_json::json!([1, 2]));
    }

    #[tokio::test]
    async fn get_returns_404_for_unknown_id() {
        let (_tmp, store, _repo) = fresh_store().await;
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/nope")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn approve_pending_transitions_to_approved() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/approve")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"decided_by":"operator"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v["status"], "approved");
        let row = repo.get(&ProposalId::new("p1")).await.unwrap();
        assert_eq!(row.status, EvolutionStatus::Approved);
        assert_eq!(row.decided_by.as_deref(), Some("operator"));
        assert!(row.decided_at.is_some());
    }

    #[tokio::test]
    async fn approve_already_decided_returns_409() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Denied))
            .await
            .unwrap();
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/approve")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"decided_by":"operator"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        let v = read_json(resp).await;
        assert_eq!(v["error"], "invalid_state_transition");
        assert_eq!(v["from"], "denied");
        assert_eq!(v["to"], "approved");
    }

    #[tokio::test]
    async fn deny_appends_reason_to_reasoning() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/deny")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"decided_by":"operator","reason":"too risky"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let row = repo.get(&ProposalId::new("p1")).await.unwrap();
        assert_eq!(row.status, EvolutionStatus::Denied);
        assert!(
            row.reasoning.contains("[DENIED: too risky]"),
            "reasoning='{}'",
            row.reasoning
        );
    }

    #[tokio::test]
    async fn deny_without_reason_keeps_reasoning_unchanged() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/deny")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"decided_by":"operator"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let row = repo.get(&ProposalId::new("p1")).await.unwrap();
        assert_eq!(row.status, EvolutionStatus::Denied);
        assert_eq!(row.reasoning, "two duplicate chunks");
    }

    #[tokio::test]
    async fn apply_only_works_from_approved() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Pending))
            .await
            .unwrap();
        let app = app_with(Some(store.clone()));
        // Pending → apply: 409.
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/apply")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);

        // Move to approved and try again.
        repo.set_decision(
            &ProposalId::new("p1"),
            EvolutionStatus::Approved,
            2_000,
            "op",
        )
        .await
        .unwrap();

        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/p1/apply")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v["status"], "applied_stub");
        let row = repo.get(&ProposalId::new("p1")).await.unwrap();
        assert_eq!(row.status, EvolutionStatus::Applied);
        assert!(row.applied_at.is_some());
    }

    #[tokio::test]
    async fn apply_unknown_id_returns_404() {
        let (_tmp, store, _repo) = fresh_store().await;
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/evolution/missing/apply")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }
}
