//! `/admin/memory/*` — operator escape hatches for the memory pipeline.
//!
//! Phase 3.1 ships exactly one route here:
//!
//! - `POST /admin/memory/decay/reset` — body
//!   `{"chunk_id": <i64>, "reason": "<string>"}`. Forces a chunk's
//!   `decay_score` back to 1.0 and stamps `last_recalled_at = now_ms`,
//!   leaving `consolidated_at` untouched. Records a
//!   `memory_op:decay_reset:<id>` row in `evolution_history` (with a
//!   matching synthetic proposal row carrying `decided_by = "admin:manual"`
//!   so the FK is satisfied) for an auditable trail.
//!
//! The route 503s `memory_admin_disabled` when either the kb store
//! ([`AdminState::rag_store`]) or the evolution store
//! ([`AdminState::history_repo`] / [`AdminState::proposals_repo`]) is
//! absent at boot — same shape as the rest of the evolution surface so
//! the UI can keep a single banner.

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use corlinman_evolution::{
    EvolutionHistory, EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionStatus, ProposalId,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use time::OffsetDateTime;
use uuid::Uuid;

use super::AdminState;

/// Sub-router for `/admin/memory/*`. Mounted by [`super::router_with_state`]
/// inside the admin_auth middleware.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/memory/decay/reset", post(reset_decay))
        .with_state(state)
}

#[derive(Debug, Deserialize)]
pub struct ResetRequest {
    pub chunk_id: i64,
    /// Operator-supplied free-form rationale. Stored on the synthetic
    /// proposal's `reasoning` column so the audit row reads back like
    /// `manual decay reset: <reason>`.
    #[serde(default)]
    pub reason: String,
}

#[derive(Debug, Serialize)]
pub struct ResetResponse {
    pub chunk_id: i64,
    pub history_id: i64,
    pub proposal_id: String,
    pub applied_at: i64,
}

async fn reset_decay(State(state): State<AdminState>, Json(req): Json<ResetRequest>) -> Response {
    // Subsystem availability gate — same convention the evolution admin
    // routes use: 503 when any required handle is missing.
    let Some(kb) = state.rag_store.clone() else {
        return memory_disabled("kb store not configured");
    };
    let Some(history_repo) = state.history_repo.clone() else {
        return memory_disabled("evolution store not configured");
    };
    let Some(proposals_repo) = state.proposals_repo.clone() else {
        return memory_disabled("evolution store not configured");
    };

    // Step 1: forward correction on the kb. `reset_chunk_decay` returns
    // 0 when the chunk doesn't exist; surface that as 404 so the operator
    // can correct the id without grepping logs.
    let affected = match kb.reset_chunk_decay(req.chunk_id).await {
        Ok(n) => n,
        Err(err) => {
            tracing::error!(error = %err, chunk_id = req.chunk_id, "decay reset: kb update failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "kb_update_failed", "detail": err.to_string()})),
            )
                .into_response();
        }
    };
    if affected == 0 {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "chunk_not_found", "chunk_id": req.chunk_id})),
        )
            .into_response();
    }

    // Step 2: write the audit pair. We need a real proposal row to
    // satisfy the FK on `evolution_history.proposal_id`; mint a UUID
    // tagged `manual-` so it's obvious in the proposal table that this
    // wasn't engine-generated. `decided_by = "admin:manual"` carries
    // the operator-marker the spec asks for.
    let now_ms = (OffsetDateTime::now_utc().unix_timestamp_nanos() / 1_000_000) as i64;
    let proposal_id = ProposalId(format!("manual-{}", Uuid::new_v4()));
    let target = format!("decay_reset:{}", req.chunk_id);
    let reasoning = if req.reason.trim().is_empty() {
        "manual decay reset".to_string()
    } else {
        format!("manual decay reset: {}", req.reason.trim())
    };

    let proposal = EvolutionProposal {
        id: proposal_id.clone(),
        kind: EvolutionKind::MemoryOp,
        target: target.clone(),
        diff: String::new(),
        reasoning,
        risk: EvolutionRisk::Low,
        budget_cost: 0,
        status: EvolutionStatus::Applied,
        shadow_metrics: None,
        signal_ids: Vec::new(),
        trace_ids: Vec::new(),
        created_at: now_ms,
        decided_at: Some(now_ms),
        decided_by: Some("admin:manual".to_string()),
        applied_at: Some(now_ms),
        rollback_of: None,
        eval_run_id: None,
        baseline_metrics_json: None,
        auto_rollback_at: None,
        auto_rollback_reason: None,
        metadata: None,
    };
    if let Err(err) = proposals_repo.insert(&proposal).await {
        tracing::error!(
            error = %err,
            chunk_id = req.chunk_id,
            "decay reset: proposal insert failed (kb already updated)",
        );
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "audit_write_failed",
                "detail": err.to_string(),
                "warning": "kb decay reset succeeded but audit row was not written",
            })),
        )
            .into_response();
    }

    // Step 3: history row. `inverse_diff` is empty because this is a
    // forward correction, not a regular apply with a diffable before /
    // after — there's nothing to reverse mechanically; an operator who
    // wants to "undo" reissues a different proposal.
    let history = EvolutionHistory {
        id: None,
        proposal_id: proposal_id.clone(),
        kind: EvolutionKind::MemoryOp,
        target,
        before_sha: String::new(),
        after_sha: String::new(),
        inverse_diff: String::new(),
        metrics_baseline: serde_json::Value::Null,
        applied_at: now_ms,
        rolled_back_at: None,
        rollback_reason: None,
        share_with: None,
    };
    let history_id = match history_repo.insert(&history).await {
        Ok(id) => id,
        Err(err) => {
            tracing::error!(
                error = %err,
                chunk_id = req.chunk_id,
                proposal_id = %proposal_id,
                "decay reset: history insert failed (kb + proposal already written)",
            );
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "audit_write_failed",
                    "detail": err.to_string(),
                    "warning": "kb decay reset succeeded but history row was not written",
                })),
            )
                .into_response();
        }
    };

    tracing::info!(
        chunk_id = req.chunk_id,
        proposal_id = %proposal_id,
        history_id,
        "decay reset: applied",
    );

    (
        StatusCode::OK,
        Json(ResetResponse {
            chunk_id: req.chunk_id,
            history_id,
            proposal_id: proposal_id.0,
            applied_at: now_ms,
        }),
    )
        .into_response()
}

fn memory_disabled(detail: &'static str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "memory_admin_disabled",
            "detail": detail,
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::admin::router_with_state;
    use arc_swap::ArcSwap;
    use argon2::password_hash::{PasswordHasher, SaltString};
    use argon2::Argon2;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use base64::Engine;
    use corlinman_core::config::Config;
    use corlinman_evolution::{EvolutionStore, HistoryRepo, ProposalsRepo};
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_vector::SqliteStore;
    use std::sync::Arc;
    use tempfile::tempdir;
    use tower::ServiceExt;

    fn hash_password(password: &str) -> String {
        let salt = SaltString::encode_b64(b"corlinman_test_salt_bytes_16").unwrap();
        Argon2::default()
            .hash_password(password.as_bytes(), &salt)
            .unwrap()
            .to_string()
    }

    fn basic(u: &str, p: &str) -> String {
        format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(format!("{u}:{p}"))
        )
    }

    async fn build_app() -> (axum::Router, Arc<SqliteStore>, tempfile::TempDir) {
        let dir = tempdir().unwrap();
        let kb_path = dir.path().join("kb.sqlite");
        let evo_path = dir.path().join("evolution.sqlite");
        let kb = Arc::new(SqliteStore::open(&kb_path).await.unwrap());
        let evo = Arc::new(EvolutionStore::open(&evo_path).await.unwrap());
        let history = HistoryRepo::new(evo.pool().clone());
        let proposals = ProposalsRepo::new(evo.pool().clone());

        let mut cfg = Config::default();
        cfg.admin.username = Some("admin".into());
        cfg.admin.password_hash = Some(hash_password("secret"));
        let state = AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
        .with_rag_store(kb.clone())
        .with_evolution_store(evo)
        .with_history_repo(history, proposals);
        let app = router_with_state(state);
        (app, kb, dir)
    }

    #[tokio::test]
    async fn reset_decay_404_when_chunk_missing() {
        let (app, _kb, _dir) = build_app().await;
        let body = serde_json::to_vec(&json!({
            "chunk_id": 9999,
            "reason": "test"
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/memory/decay/reset")
                    .header(header::AUTHORIZATION, basic("admin", "secret"))
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn reset_decay_succeeds_and_writes_audit() {
        let (app, kb, _dir) = build_app().await;
        // Insert a file + chunk, then force decay_score down so we can
        // verify the reset bumped it back to 1.0.
        let file_id = kb
            .insert_file("/tmp/test.md", "diary", "sha", 0, 0)
            .await
            .unwrap();
        let id = kb
            .insert_chunk(file_id, 0, "hello world", None, "general")
            .await
            .unwrap();
        sqlx::query("UPDATE chunks SET decay_score = 0.2 WHERE id = ?")
            .bind(id)
            .execute(kb.pool())
            .await
            .unwrap();

        let body = serde_json::to_vec(&json!({
            "chunk_id": id,
            "reason": "operator override after false decay"
        }))
        .unwrap();
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/memory/decay/reset")
                    .header(header::AUTHORIZATION, basic("admin", "secret"))
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["chunk_id"], id);
        assert!(v["history_id"].as_i64().unwrap() > 0);
        assert!(v["proposal_id"].as_str().unwrap().starts_with("manual-"));

        let state = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert!((state.decay_score - 1.0).abs() < 1e-5);
        assert!(state.last_recalled_at.is_some());
    }
}
