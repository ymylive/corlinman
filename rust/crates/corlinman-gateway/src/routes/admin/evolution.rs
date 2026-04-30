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
use corlinman_core::metrics::{EVOLUTION_PROPOSALS_DECISION, EVOLUTION_PROPOSALS_LISTED};
use corlinman_evolution::{
    iso_week_window, EvolutionKind, EvolutionProposal, EvolutionStatus, EvolutionStore, ProposalId,
    ProposalsRepo, RepoError,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sqlx::SqlitePool;
use std::str::FromStr;
use tracing::warn;

use super::AdminState;
use crate::evolution_applier::{ApplyError, EvolutionApplier};

/// Default `?limit=` when the caller doesn't pass one. Same ballpark as
/// `/admin/approvals` (no explicit limit there, but the UI batches in 50s).
const DEFAULT_LIMIT: i64 = 50;

/// Hard ceiling on `?limit=` so a buggy client can't pull the whole table.
const MAX_LIMIT: i64 = 200;

/// Sub-router for `/admin/evolution*`. Mounted by
/// [`super::router_with_state`] inside the admin_auth middleware.
pub fn router(state: AdminState) -> Router {
    // `/admin/evolution/budget` is registered before `/admin/evolution/:id`
    // so the literal path wins the axum router match (otherwise the
    // wildcard captures `budget` and tries to look up a proposal of
    // that id).
    Router::new()
        .route("/admin/evolution", get(list_proposals))
        .route("/admin/evolution/budget", get(budget_snapshot))
        .route("/admin/evolution/history", get(list_history))
        .route("/admin/evolution/:id", get(get_proposal))
        .route("/admin/evolution/:id/approve", post(approve_proposal))
        .route("/admin/evolution/:id/deny", post(deny_proposal))
        .route("/admin/evolution/:id/apply", post(apply_proposal))
        // Phase 4 W1.5 (next-tasks A2): operator-initiated rollback.
        // AutoRollback monitors call `EvolutionApplier::revert`
        // programmatically; this surface is the manual-action path
        // for the admin UI's Rollback button.
        .route("/admin/evolution/:id/rollback", post(rollback_proposal))
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
    // ─── W1-A / W1-B: shadow + auto-rollback context ─────────────────
    /// Eval run identifier captured by the ShadowTester at shadow_done.
    pub eval_run_id: Option<String>,
    /// Pre-shadow baseline metrics (MetricSnapshot JSON). Lets the UI
    /// render a baseline-vs-shadow delta on Approved cards.
    pub baseline_metrics_json: Option<serde_json::Value>,
    /// Unix-millis at which the AutoRollback monitor flipped this row
    /// from `applied → rolled_back`. Null for the manual-rollback path.
    pub auto_rollback_at: Option<i64>,
    /// Human-readable threshold-breach reason carried from the monitor.
    pub auto_rollback_reason: Option<String>,
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
            eval_run_id: p.eval_run_id,
            baseline_metrics_json: p.baseline_metrics_json,
            auto_rollback_at: p.auto_rollback_at,
            auto_rollback_reason: p.auto_rollback_reason,
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

/// One row of the per-kind quota table. `limit` is the configured
/// `[evolution.budget.per_kind.<kind>]` cap (zero entries are filtered
/// out upstream); `used` is the live count of proposals filed in the
/// current ISO week with that kind; `remaining` is `max(limit - used, 0)`
/// so the UI gauge never renders a negative value when the engine
/// briefly overshot before the gate noticed.
#[derive(Debug, Serialize)]
pub struct BudgetKindRow {
    pub kind: String,
    pub limit: u32,
    pub used: u32,
    pub remaining: u32,
}

/// Aggregate weekly_total quota — same triple as `BudgetKindRow` minus
/// the kind label.
#[derive(Debug, Serialize)]
pub struct BudgetTotal {
    pub limit: u32,
    pub used: u32,
    pub remaining: u32,
}

/// Wire shape for `GET /admin/evolution/budget`. Both the engine (for
/// pre-flight checks) and the UI gauge consume this; the field names
/// are pinned in the wave 1-C contract.
#[derive(Debug, Serialize)]
pub struct BudgetSnapshot {
    pub enabled: bool,
    pub window_start_ms: i64,
    pub window_end_ms: i64,
    pub weekly_total: BudgetTotal,
    /// Sorted alphabetically by `kind` so diffs across snapshots stay
    /// stable. Kinds present in `[evolution.budget.per_kind]` with a
    /// limit of 0 are filtered (an explicit zero cap means "block this
    /// kind entirely" — the engine handles that without a row in the
    /// snapshot).
    pub per_kind: Vec<BudgetKindRow>,
}

fn saturating_remaining(limit: u32, used: u32) -> u32 {
    limit.saturating_sub(used)
}

async fn budget_snapshot(State(state): State<AdminState>) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let (repo, _) = resolve_handles(store);
    let cfg = state.config.load_full();
    let budget_cfg = &cfg.evolution.budget;
    let now = now_ms();
    let (window_start_ms, window_end_ms) = iso_week_window(now);

    let weekly_used = match repo.count_proposals_in_iso_week(now, None).await {
        Ok(n) => n,
        Err(err) => return storage_error(err, "budget.count_total"),
    };

    // Walk per-kind in `BTreeMap` order (already alphabetical by enum
    // ordering — the Default config order matches snake_case sort —
    // but re-sort by serialized `kind` string to make the contract
    // explicit and survive future reorderings of the enum).
    let mut rows: Vec<BudgetKindRow> = Vec::with_capacity(budget_cfg.per_kind.len());
    for (kind, limit) in budget_cfg.per_kind.iter() {
        if *limit == 0 {
            continue;
        }
        let used = match repo.count_proposals_in_iso_week(now, Some(*kind)).await {
            Ok(n) => n,
            Err(err) => return storage_error(err, "budget.count_kind"),
        };
        rows.push(BudgetKindRow {
            kind: kind.as_str().to_string(),
            limit: *limit,
            used,
            remaining: saturating_remaining(*limit, used),
        });
    }
    rows.sort_by(|a, b| a.kind.cmp(&b.kind));

    let snapshot = BudgetSnapshot {
        enabled: budget_cfg.enabled,
        window_start_ms,
        window_end_ms,
        weekly_total: BudgetTotal {
            limit: budget_cfg.weekly_total,
            used: weekly_used,
            remaining: saturating_remaining(budget_cfg.weekly_total, weekly_used),
        },
        per_kind: rows,
    };
    Json(snapshot).into_response()
}

async fn apply_proposal(State(state): State<AdminState>, Path(id): Path<String>) -> Response {
    // Both `evolution_store` (read path) and `evolution_applier` (write
    // path) must be wired for `/apply` to function. Treating the
    // missing-applier case as 503 `evolution_disabled` keeps the UI to
    // one banner regardless of which subsystem is unconfigured.
    let Some(applier) = state.evolution_applier.as_ref() else {
        return evolution_disabled();
    };
    let pid = ProposalId::new(id.clone());
    match applier.apply(&pid).await {
        Ok(history) => Json(json!({
            "id": id,
            "status": EvolutionStatus::Applied.as_str(),
            "history_id": history.id,
        }))
        .into_response(),
        Err(ApplyError::NotFound(_)) => not_found(&id),
        Err(ApplyError::NotApproved(actual)) => {
            // Mirror the pre-Wave-2-A 409 contract — clients already
            // depend on the `invalid_state_transition` shape from the
            // approve / deny routes.
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            let from = EvolutionStatus::from_str(&actual).unwrap_or(EvolutionStatus::Pending);
            invalid_state_transition(from, EvolutionStatus::Applied)
        }
        Err(ApplyError::UnsupportedKind(kind_str)) => {
            // Map `kind_str` back to a typed enum for the metrics call.
            // Unknown strings (shouldn't happen — value came from the
            // typed `EvolutionKind`) fall back to `MemoryOp` so the
            // counter still moves.
            let kind = EvolutionKind::from_str(&kind_str).unwrap_or(EvolutionKind::MemoryOp);
            EvolutionApplier::observe_failure(kind);
            (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "unsupported_kind",
                    "kind": kind_str,
                    "message": "no forward handler for this kind yet",
                })),
            )
                .into_response()
        }
        Err(ApplyError::InvalidTarget(target)) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "invalid_target",
                    "target": target,
                })),
            )
                .into_response()
        }
        Err(ApplyError::ChunkNotFound(chunk_id)) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            (
                StatusCode::CONFLICT,
                Json(json!({
                    "error": "chunk_not_found",
                    "chunk_id": chunk_id,
                })),
            )
                .into_response()
        }
        // Phase 3-2B: tag_rebalance / skill_update validation failures.
        // 422 for state-shape problems the proposer should have caught
        // (root merge attempt, missing tag/file); 400 for diff shapes
        // we don't accept yet — same semantic split as the existing
        // 4xx mappings above.
        Err(ApplyError::TagNotFound(path)) => {
            EvolutionApplier::observe_failure(EvolutionKind::TagRebalance);
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": "tag_not_found",
                    "path": path,
                })),
            )
                .into_response()
        }
        Err(ApplyError::CannotMergeRoot) => {
            EvolutionApplier::observe_failure(EvolutionKind::TagRebalance);
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": "cannot_merge_root",
                })),
            )
                .into_response()
        }
        Err(ApplyError::SkillFileMissing(path)) => {
            EvolutionApplier::observe_failure(EvolutionKind::SkillUpdate);
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": "skill_file_missing",
                    "path": path,
                })),
            )
                .into_response()
        }
        Err(ApplyError::UnsupportedDiffShape(detail)) => {
            EvolutionApplier::observe_failure(EvolutionKind::SkillUpdate);
            (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "unsupported_diff_shape",
                    "detail": detail,
                })),
            )
                .into_response()
        }
        // Phase 4 W1.5 (next-tasks A3): tool_policy drift detection
        // surfaces the on-disk mode that diverged from `diff.before`.
        // Operator can re-evaluate without re-querying. 409 mirrors
        // `invalid_state_transition` semantically — both are
        // "stale-precondition" failures.
        Err(ApplyError::DriftMismatch {
            target,
            expected,
            actual,
        }) => {
            EvolutionApplier::observe_failure(EvolutionKind::ToolPolicy);
            (
                StatusCode::CONFLICT,
                Json(json!({
                    "error": "drift_mismatch",
                    "target": target,
                    "expected": expected,
                    "actual": actual,
                })),
            )
                .into_response()
        }
        Err(other) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            warn!(error = %other, "admin/evolution apply failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "apply_failed",
                    "message": other.to_string(),
                })),
            )
                .into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// Rollback — `POST /admin/evolution/:id/rollback`
// ---------------------------------------------------------------------------
//
// Phase 4 W1.5 (next-tasks A2): operator-initiated rollback. The
// AutoRollback monitor already drives `EvolutionApplier::revert`
// programmatically when post-apply metrics breach the threshold; this
// route is the manual-action path so an operator can trigger the same
// flow from the admin UI without an out-of-band SQL surgery.
//
// State machine:
//
// ```text
// applied ──► rolled_back   (this route)
// applied ──► rolled_back   (AutoRollback monitor; same code path)
// ```
//
// Any non-`applied` row returns 409 `invalid_state_transition` matching
// the existing `apply` / `approve` envelope. Reverting an already-
// rolled-back row is intentionally NOT idempotent — the applier
// returns `NotApplied("rolled_back")` so the operator sees the
// double-fire and doesn't accidentally re-apply the original change.

#[derive(Debug, Deserialize, Default)]
pub struct RollbackBody {
    /// Optional human-readable reason recorded in
    /// `evolution_proposals.auto_rollback_reason`. Defaults to
    /// `"operator: <username unknown>"` when absent so the audit
    /// log always carries something. The `decided_by` claim from
    /// the admin session would be a better source; threading that
    /// through is a Phase 4 follow-up alongside the chat-lifecycle
    /// tenant work.
    #[serde(default)]
    pub reason: Option<String>,
}

async fn rollback_proposal(
    State(state): State<AdminState>,
    Path(id): Path<String>,
    body: Option<Json<RollbackBody>>,
) -> Response {
    let Some(applier) = state.evolution_applier.as_ref() else {
        return evolution_disabled();
    };
    let pid = ProposalId::new(id.clone());
    let reason = body
        .and_then(|Json(b)| b.reason)
        .unwrap_or_else(|| "operator: unknown".to_string());

    match applier.revert(&pid, &reason).await {
        Ok(history) => Json(json!({
            "id": id,
            "status": EvolutionStatus::RolledBack.as_str(),
            "history_id": history.id,
            "reason": reason,
        }))
        .into_response(),
        Err(ApplyError::NotFound(_)) => not_found(&id),
        Err(ApplyError::NotApplied(actual)) => {
            // Distinct envelope from `NotApproved`'s 409 because the
            // forward state machine uses `Applied → RolledBack` and
            // the UI should distinguish "never applied" from "already
            // rolled back".
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            let from = EvolutionStatus::from_str(&actual).unwrap_or(EvolutionStatus::Pending);
            invalid_state_transition(from, EvolutionStatus::RolledBack)
        }
        Err(ApplyError::UnsupportedRevertKind(kind_str)) => {
            let kind = EvolutionKind::from_str(&kind_str).unwrap_or(EvolutionKind::MemoryOp);
            EvolutionApplier::observe_failure(kind);
            (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "unsupported_revert_kind",
                    "kind": kind_str,
                    "message": "no inverse handler for this kind yet",
                })),
            )
                .into_response()
        }
        Err(ApplyError::HistoryMissing(pid_str)) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            // 410 Gone semantically captures "the audit row that
            // would have driven this revert is missing"; the
            // forward apply succeeded but the history is corrupt.
            (
                StatusCode::GONE,
                Json(json!({
                    "error": "history_missing",
                    "proposal_id": pid_str,
                    "message": "evolution_history row missing for this proposal; cannot revert without inverse_diff",
                })),
            )
                .into_response()
        }
        Err(ApplyError::Tampered(reason)) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            (
                StatusCode::CONFLICT,
                Json(json!({
                    "error": "tampered_inverse_diff",
                    "reason": reason,
                    "message": "inverse_diff failed trust gates; manual reconciliation required",
                })),
            )
                .into_response()
        }
        Err(other) => {
            EvolutionApplier::observe_failure(EvolutionKind::MemoryOp);
            warn!(error = %other, "admin/evolution rollback failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "rollback_failed",
                    "message": other.to_string(),
                })),
            )
                .into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// History — `GET /admin/evolution/history`
// ---------------------------------------------------------------------------
//
// Joins `evolution_history` against `evolution_proposals` so the UI's
// History tab can render terminal-state proposals (applied + rolled_back)
// with full reasoning, baseline metrics, and the post-apply MetricSnapshot
// captured by the W1-B applier — all in one round-trip.

/// Query params for `GET /admin/evolution/history`. `limit` defaults to
/// 50 and is clamped at the same MAX_LIMIT (200) as the proposal list so
/// no client can yank the entire audit log in one fetch.
#[derive(Debug, Deserialize, Default)]
pub struct HistoryQuery {
    #[serde(default)]
    pub limit: Option<i64>,
}

/// Wire shape for one row in `GET /admin/evolution/history`.
///
/// Carries both `metrics_baseline` (the W1-B `MetricSnapshot` written at
/// apply time) and `shadow_metrics` (W1-A pre-apply shadow run output)
/// so the UI's `MetricsDelta` viz can render baseline-vs-shadow on
/// rolled-back rows without a follow-up fetch.
#[derive(Debug, Serialize)]
pub struct HistoryEntryOut {
    pub proposal_id: String,
    pub kind: String,
    pub target: String,
    pub risk: String,
    /// Either "applied" or "rolled_back" — mirrors the proposals row.
    pub status: String,
    pub applied_at: i64,
    pub rolled_back_at: Option<i64>,
    /// Operator-supplied reason (history table). Distinct from
    /// `auto_rollback_reason` which the AutoRollback monitor stamps.
    pub rollback_reason: Option<String>,
    pub auto_rollback_reason: Option<String>,
    pub metrics_baseline: serde_json::Value,
    pub shadow_metrics: Option<serde_json::Value>,
    pub baseline_metrics_json: Option<serde_json::Value>,
    pub before_sha: String,
    pub after_sha: String,
    pub eval_run_id: Option<String>,
    pub reasoning: String,
}

async fn list_history(State(state): State<AdminState>, Query(q): Query<HistoryQuery>) -> Response {
    let Some(store) = state.evolution_store.as_ref() else {
        return evolution_disabled();
    };
    let limit = q.limit.unwrap_or(DEFAULT_LIMIT).clamp(1, MAX_LIMIT);
    let pool = store.pool();

    // One JOINed pull — the UI never wants the proposals row without the
    // history row (history holds the audit trail) so do the join in SQL
    // rather than two round-trips.
    let rows = match sqlx::query(
        r#"SELECT h.proposal_id, p.kind, p.target, p.risk, p.status,
                  h.applied_at, h.rolled_back_at, h.rollback_reason,
                  p.auto_rollback_reason, h.metrics_baseline,
                  p.shadow_metrics, p.baseline_metrics_json,
                  h.before_sha, h.after_sha, p.eval_run_id, p.reasoning
             FROM evolution_history h
             JOIN evolution_proposals p ON p.id = h.proposal_id
            ORDER BY h.applied_at DESC
            LIMIT ?"#,
    )
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(err) => {
            warn!(error = %err, "admin/evolution history.fetch failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "storage_error",
                    "message": err.to_string(),
                })),
            )
                .into_response();
        }
    };

    let mut out: Vec<HistoryEntryOut> = Vec::with_capacity(rows.len());
    for row in rows {
        use sqlx::Row as _;
        // metrics_baseline is NOT NULL in the schema; the others are
        // optional. Bad JSON is a 500 — better to surface the corrupt
        // row than to silently return a misleading payload.
        let metrics_baseline_str: String = row.get("metrics_baseline");
        let metrics_baseline: serde_json::Value = match serde_json::from_str(&metrics_baseline_str)
        {
            Ok(v) => v,
            Err(err) => {
                warn!(error = %err, "history.metrics_baseline malformed json");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({
                        "error": "storage_error",
                        "message": format!("metrics_baseline: {err}"),
                    })),
                )
                    .into_response();
            }
        };
        let shadow_metrics = match row.get::<Option<String>, _>("shadow_metrics") {
            Some(s) => match serde_json::from_str::<serde_json::Value>(&s) {
                Ok(v) => Some(v),
                Err(err) => {
                    warn!(error = %err, "history.shadow_metrics malformed json");
                    None
                }
            },
            None => None,
        };
        let baseline_metrics_json = match row.get::<Option<String>, _>("baseline_metrics_json") {
            Some(s) => match serde_json::from_str::<serde_json::Value>(&s) {
                Ok(v) => Some(v),
                Err(err) => {
                    warn!(error = %err, "history.baseline_metrics_json malformed");
                    None
                }
            },
            None => None,
        };
        out.push(HistoryEntryOut {
            proposal_id: row.get("proposal_id"),
            kind: row.get("kind"),
            target: row.get("target"),
            risk: row.get("risk"),
            status: row.get("status"),
            applied_at: row.get("applied_at"),
            rolled_back_at: row.get("rolled_back_at"),
            rollback_reason: row.get("rollback_reason"),
            auto_rollback_reason: row.get("auto_rollback_reason"),
            metrics_baseline,
            shadow_metrics,
            baseline_metrics_json,
            before_sha: row.get("before_sha"),
            after_sha: row.get("after_sha"),
            eval_run_id: row.get("eval_run_id"),
            reasoning: row.get("reasoning"),
        });
    }
    Json(out).into_response()
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
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
        }
    }

    fn app_with(store: Option<Arc<EvolutionStore>>) -> Router {
        app_with_full(store, None)
    }

    /// Variant that also accepts an `EvolutionApplier`. Wave 2-A apply
    /// tests need a real applier — earlier list/approve/deny tests don't
    /// touch the apply route and pass `None`.
    fn app_with_full(
        store: Option<Arc<EvolutionStore>>,
        applier: Option<Arc<EvolutionApplier>>,
    ) -> Router {
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
            evolution_applier: applier,
            history_repo: None,
            proposals_repo: None,
            tenant_pool: None,
            allowed_tenants: std::collections::BTreeSet::new(),
            admin_db: None,
        };
        router(state)
    }

    /// Build a kb store + applier wired against the given evolution
    /// store. Returns the kb store so individual tests can seed chunks
    /// before calling `/apply`.
    async fn build_applier(
        tmp: &TempDir,
        evol: Arc<EvolutionStore>,
    ) -> (Arc<corlinman_vector::SqliteStore>, Arc<EvolutionApplier>) {
        let kb_path = tmp.path().join("kb.sqlite");
        let kb = Arc::new(corlinman_vector::SqliteStore::open(&kb_path).await.unwrap());
        let skills_dir = tmp.path().join("skills");
        let applier = Arc::new(EvolutionApplier::new(
            evol,
            kb.clone(),
            corlinman_core::config::AutoRollbackThresholds::default(),
            skills_dir,
        ));
        (kb, applier)
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
        let (tmp, store, repo) = fresh_store().await;
        let (kb, applier) = build_applier(&tmp, store.clone()).await;
        // Seed two chunks so the real merge_chunks pipeline finds rows
        // when the proposal flips to approved.
        let file_id = kb.insert_file("/t", "diary", "ck", 0, 0).await.unwrap();
        let a = kb
            .insert_chunk(file_id, 0, "winner", None, "general")
            .await
            .unwrap();
        let b = kb
            .insert_chunk(file_id, 1, "loser", None, "general")
            .await
            .unwrap();
        let target = format!("merge_chunks:{a},{b}");

        let mut p = proposal("p1", EvolutionStatus::Pending);
        p.target = target;
        repo.insert(&p).await.unwrap();

        let app = app_with_full(Some(store.clone()), Some(applier.clone()));
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

        let app = app_with_full(Some(store), Some(applier));
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
        assert_eq!(v["status"], "applied");
        assert!(v["history_id"].is_i64());
        let row = repo.get(&ProposalId::new("p1")).await.unwrap();
        assert_eq!(row.status, EvolutionStatus::Applied);
        assert!(row.applied_at.is_some());

        // Loser chunk gone; winner kept.
        let rows = kb.query_chunks_by_ids(&[a, b]).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, a);
    }

    #[tokio::test]
    async fn apply_unknown_id_returns_404() {
        let (tmp, store, _repo) = fresh_store().await;
        let (_kb, applier) = build_applier(&tmp, store.clone()).await;
        let app = app_with_full(Some(store), Some(applier));
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

    #[tokio::test]
    async fn apply_returns_503_when_applier_missing() {
        let (_tmp, store, repo) = fresh_store().await;
        repo.insert(&proposal("p1", EvolutionStatus::Approved))
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
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    // ---------------------------------------------------------------
    // History endpoint — `GET /admin/evolution/history`
    // ---------------------------------------------------------------

    /// Helper: insert a history row for `pid` with explicit timestamps so
    /// the limit / ordering tests can pin row order without touching the
    /// real applier.
    async fn seed_history_row(
        pool: &SqlitePool,
        pid: &str,
        applied_at: i64,
        rolled_back_at: Option<i64>,
        rollback_reason: Option<&str>,
    ) {
        sqlx::query(
            r#"INSERT INTO evolution_history
                 (proposal_id, kind, target, before_sha, after_sha,
                  inverse_diff, metrics_baseline, applied_at,
                  rolled_back_at, rollback_reason)
               VALUES (?, 'memory_op', 'merge_chunks:1,2', 'sha-before', 'sha-after',
                       '{}', '{"target":"merge_chunks:1,2","counts":{"tool.call.failed":3}}',
                       ?, ?, ?)"#,
        )
        .bind(pid)
        .bind(applied_at)
        .bind(rolled_back_at)
        .bind(rollback_reason)
        .execute(pool)
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn history_empty_returns_200_and_empty_array() {
        let (_tmp, store, _repo) = fresh_store().await;
        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/history")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v.as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn history_returns_applied_and_rolled_back_rows_newest_first() {
        let (_tmp, store, repo) = fresh_store().await;
        // Seed two proposals: one applied, one rolled_back. Use distinct
        // applied_at so the DESC ordering is observable.
        let mut p1 = proposal("p-applied", EvolutionStatus::Applied);
        p1.applied_at = Some(2_000);
        repo.insert(&p1).await.unwrap();
        let mut p2 = proposal("p-rolled", EvolutionStatus::RolledBack);
        p2.applied_at = Some(3_000);
        repo.insert(&p2).await.unwrap();

        let pool = store.pool().clone();
        seed_history_row(&pool, "p-applied", 2_000, None, None).await;
        seed_history_row(
            &pool,
            "p-rolled",
            3_000,
            Some(4_000),
            Some("metrics regression"),
        )
        .await;

        let app = app_with(Some(store));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/history")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        let arr = v.as_array().unwrap();
        assert_eq!(arr.len(), 2);
        // Newest applied_at first.
        assert_eq!(arr[0]["proposal_id"], "p-rolled");
        assert_eq!(arr[0]["status"], "rolled_back");
        assert_eq!(arr[0]["rollback_reason"], "metrics regression");
        assert_eq!(arr[1]["proposal_id"], "p-applied");
        assert_eq!(arr[1]["status"], "applied");
        // metrics_baseline is decoded back into an object, not a string.
        assert!(arr[0]["metrics_baseline"].is_object());
    }

    #[tokio::test]
    async fn history_clamps_limit_param() {
        let (_tmp, store, repo) = fresh_store().await;
        // Seed 3 proposals + history rows.
        for i in 0..3 {
            let id = format!("p-h-{i}");
            let mut p = proposal(&id, EvolutionStatus::Applied);
            p.applied_at = Some(1_000 + i as i64);
            repo.insert(&p).await.unwrap();
            seed_history_row(store.pool(), &id, 1_000 + i as i64, None, None).await;
        }

        let app = app_with(Some(store));
        // limit=1 must clamp upward (not downward) — return only the
        // newest one.
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/history?limit=1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = read_json(resp).await;
        assert_eq!(v.as_array().unwrap().len(), 1);
        assert_eq!(v[0]["proposal_id"], "p-h-2");
    }

    #[tokio::test]
    async fn history_returns_503_when_store_missing() {
        let app = app_with(None);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/evolution/history")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }
}
