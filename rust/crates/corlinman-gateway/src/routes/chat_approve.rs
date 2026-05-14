//! `POST /v1/chat/completions/:turn_id/approve` — per-turn tool-approval
//! relay (Phase 4 W3 C4 iter 3).
//!
//! When the agent emits an `AwaitingApproval` frame mid-stream
//! (`agent.proto:137-143`), today the only way for an external client
//! to push the decision back is via the admin route
//! `/admin/approvals/:id/decide`. That works for the web UI (which
//! holds an admin session cookie), but is awkward for native clients
//! that authenticate with a chat-scoped api_key — they shouldn't need
//! admin credentials to answer their own approval prompt.
//!
//! This module adds a parallel public surface scoped to `/v1/*` so the
//! same Bearer token used for `/v1/chat/completions` can also satisfy
//! the in-stream approval round trip:
//!
//! ```text
//! POST /v1/chat/completions/{turn_id}/approve
//! Authorization: Bearer <chat-scoped api_key>      [enforced once
//!                                                    middleware lands;
//!                                                    iter 3 ships the
//!                                                    surface only]
//! Content-Type: application/json
//!
//! {
//!   "call_id": "call_abc123",
//!   "approved": true,
//!   "scope": "once",                  // "once" | "session" | "always"
//!   "deny_message": "explain why..."  // required when approved=false
//! }
//! ```
//!
//! Response on success:
//!
//! ```json
//! { "turn_id": "...", "call_id": "call_abc123", "decision": "approved" }
//! ```
//!
//! ### Scope handling — iter 3 stub
//!
//! The body's `scope` field is captured and echoed back on the
//! response, but at iter 3 the gateway only forwards a binary
//! `Approved` / `Denied(reason)` decision through `ApprovalGate::resolve`.
//! Tracking `session` / `always` decisions across calls is part of iter
//! 9 (the ApprovalSheet on the Swift side) — until then operators see
//! the same prompt every turn, which matches the existing admin-route
//! behaviour. The body is forward-compatible: future iterations widen
//! `ApprovalDecision` to carry scope without changing this wire shape.
//!
//! ### Why `turn_id` if the gate keys off `call_id`?
//!
//! `turn_id` is the request-side correlation id surfaced in the SSE
//! stream's `ChatStart` frame (`chat.rs:1` references the OpenAI-style
//! `id` field). Native clients store it alongside the in-flight
//! AwaitingApproval prompt so a single client can pre-flight the
//! approve POST against a logically-current turn even before
//! `call_id` arrives. We currently route on `call_id` alone —
//! `turn_id` is recorded in the response for correlation and reserved
//! for future per-turn quotas / replay.
//!
//! ### Disabled / not-found paths
//!
//! - **503 `approvals_disabled`** when `ChatState::approval_gate` is
//!   `None` (mirrors `/admin/approvals*` envelope).
//! - **400 `invalid_request`** when `call_id` is empty or
//!   `approved == false` with no `deny_message`.
//! - **404 `not_found`** when the call_id doesn't match a pending row.

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::warn;

use crate::middleware::approval::ApprovalDecision;
use crate::routes::chat::ChatState;

/// Body shape for `POST /v1/chat/completions/:turn_id/approve`.
#[derive(Debug, Deserialize)]
pub struct ApproveBody {
    pub call_id: String,
    pub approved: bool,
    #[serde(default)]
    pub scope: Option<String>,
    #[serde(default)]
    pub deny_message: Option<String>,
}

/// Wire shape on success. Mirrors the admin-route `decide_approval`
/// envelope but adds `turn_id` + `call_id` for correlation on the
/// client side.
#[derive(Debug, Serialize)]
pub struct ApproveResponse {
    pub turn_id: String,
    pub call_id: String,
    pub decision: &'static str,
    pub scope: Option<String>,
}

/// Mount the per-turn approve route onto a router. Composed by
/// [`super::chat::router_with_state`] so callers don't need to know
/// the URL shape.
pub fn router_with_state(state: ChatState) -> Router {
    Router::new()
        .route(
            "/v1/chat/completions/:turn_id/approve",
            post(handle_approve),
        )
        .with_state(state)
}

async fn handle_approve(
    State(state): State<ChatState>,
    Path(turn_id): Path<String>,
    Json(body): Json<ApproveBody>,
) -> Response {
    let Some(gate) = state.approval_gate.as_ref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "approvals_disabled",
                "message": "approval gate is not configured on this gateway",
            })),
        )
            .into_response();
    };

    let call_id = body.call_id.trim();
    if call_id.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid_request",
                "message": "`call_id` is required and must be non-empty",
            })),
        )
            .into_response();
    }

    if !body.approved
        && body
            .deny_message
            .as_deref()
            .map(str::trim)
            .unwrap_or("")
            .is_empty()
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid_request",
                "message": "`deny_message` is required when approved=false",
            })),
        )
            .into_response();
    }

    let decision = if body.approved {
        ApprovalDecision::Approved
    } else {
        ApprovalDecision::Denied(body.deny_message.clone().unwrap_or_default())
    };

    match gate.resolve(call_id, decision.clone()).await {
        Ok(()) => {
            // We construct `decision` ourselves above so only `Approved`
            // and `Denied(_)` are reachable; `Timeout` is reserved for
            // the gate's own deadline path. The `unreachable!` arm
            // documents the invariant rather than papering over with a
            // catch-all that could mask a future enum addition.
            let label: &'static str = match decision {
                ApprovalDecision::Approved => "approved",
                ApprovalDecision::Denied(_) => "denied",
                ApprovalDecision::Timeout => unreachable!(
                    "Timeout is internal to the gate; client-supplied \
                     decisions are Approved or Denied only"
                ),
            };
            (
                StatusCode::OK,
                Json(ApproveResponse {
                    turn_id,
                    call_id: call_id.to_string(),
                    decision: label,
                    scope: body.scope,
                }),
            )
                .into_response()
        }
        Err(corlinman_core::CorlinmanError::NotFound { .. }) => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "not_found",
                "resource": "approval",
                "call_id": call_id,
                "turn_id": turn_id,
            })),
        )
            .into_response(),
        Err(err) => {
            warn!(error = %err, call_id = %call_id, turn_id = %turn_id, "v1 chat approve: resolve failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "approve_failed",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

/* ------------------------------------------------------------------ */
/*                              Tests                                  */
/* ------------------------------------------------------------------ */

#[cfg(test)]
mod tests {
    use super::*;
    use crate::middleware::approval::ApprovalGate;
    use crate::routes::chat::{BackendRx, ChatBackend, ChatState};
    use async_trait::async_trait;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use corlinman_core::CorlinmanError;
    use corlinman_proto::v1::{ChatStart, ClientFrame};
    use corlinman_vector::{PendingApproval, SqliteStore};
    use std::sync::Arc;
    use std::time::Duration;
    use tempfile::TempDir;
    use tokio::sync::mpsc;
    use tower::ServiceExt;

    /// Stub backend so `ChatState::new` can be constructed. The approve
    /// route never invokes `start` — its only contact with `ChatState`
    /// is reading the `approval_gate` field.
    struct StubBackend;

    #[async_trait]
    impl ChatBackend for StubBackend {
        async fn start(
            &self,
            _start: ChatStart,
        ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
            unreachable!("approve route does not invoke the chat backend")
        }
    }

    /// Build a `(router, kept_tmp, call_id)` triple where `tmp` must be
    /// kept alive for the duration of the test (sqlite WAL files).
    async fn fixture_with_pending_call() -> (Router, TempDir, String) {
        let tmp = TempDir::new().unwrap();
        let db_path = tmp.path().join("approvals.sqlite");
        let store = Arc::new(SqliteStore::open(&db_path).await.unwrap());

        let gate = Arc::new(ApprovalGate::new(
            Vec::new(),
            store.clone(),
            Duration::from_secs(60),
        ));

        let call_id = "call_abc123".to_string();
        store
            .insert_pending_approval(&PendingApproval {
                id: call_id.clone(),
                session_key: "test_session".to_string(),
                plugin: "test_plugin".to_string(),
                tool: "test_tool".to_string(),
                args_json: "{}".to_string(),
                requested_at: "2026-05-08T00:00:00Z".to_string(),
                decided_at: None,
                decision: None,
            })
            .await
            .unwrap();

        let backend: Arc<dyn ChatBackend> = Arc::new(StubBackend);
        let state = ChatState::new(backend).with_approval_gate(gate);
        let app = router_with_state(state);
        (app, tmp, call_id)
    }

    fn json_req(uri: &str, body: serde_json::Value) -> Request<Body> {
        Request::builder()
            .method("POST")
            .uri(uri)
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(serde_json::to_vec(&body).unwrap()))
            .unwrap()
    }

    #[tokio::test]
    async fn approve_resolves_pending_call_and_echoes_scope() {
        let (app, _tmp, call_id) = fixture_with_pending_call().await;

        let resp = app
            .oneshot(json_req(
                &"/v1/chat/completions/turn_42/approve".to_string(),
                serde_json::json!({
                    "call_id": call_id,
                    "approved": true,
                    "scope": "session",
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["turn_id"], "turn_42");
        assert_eq!(v["call_id"], call_id);
        assert_eq!(v["decision"], "approved");
        assert_eq!(v["scope"], "session");
    }

    #[tokio::test]
    async fn deny_requires_deny_message() {
        let (app, _tmp, call_id) = fixture_with_pending_call().await;
        let resp = app
            .oneshot(json_req(
                "/v1/chat/completions/turn_42/approve",
                serde_json::json!({
                    "call_id": call_id,
                    "approved": false,
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"], "invalid_request");
    }

    #[tokio::test]
    async fn empty_call_id_is_400() {
        let (app, _tmp, _call_id) = fixture_with_pending_call().await;
        let resp = app
            .oneshot(json_req(
                "/v1/chat/completions/turn_42/approve",
                serde_json::json!({
                    "call_id": "  ",
                    "approved": true,
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn unknown_call_id_is_404() {
        let (app, _tmp, _call_id) = fixture_with_pending_call().await;
        let resp = app
            .oneshot(json_req(
                "/v1/chat/completions/turn_42/approve",
                serde_json::json!({
                    "call_id": "call_does_not_exist",
                    "approved": true,
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"], "not_found");
        assert_eq!(v["resource"], "approval");
    }

    #[tokio::test]
    async fn missing_approval_gate_is_503() {
        let backend: Arc<dyn ChatBackend> = Arc::new(StubBackend);
        // ChatState::new(...) starts with `approval_gate = None`.
        let state = ChatState::new(backend);
        let app = router_with_state(state);

        let resp = app
            .oneshot(json_req(
                "/v1/chat/completions/turn_42/approve",
                serde_json::json!({
                    "call_id": "anything",
                    "approved": true,
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"], "approvals_disabled");
    }

    #[tokio::test]
    async fn deny_with_message_resolves_to_denied() {
        let (app, _tmp, call_id) = fixture_with_pending_call().await;
        let resp = app
            .oneshot(json_req(
                "/v1/chat/completions/turn_42/approve",
                serde_json::json!({
                    "call_id": call_id,
                    "approved": false,
                    "deny_message": "operator vetoed",
                }),
            ))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = to_bytes(resp.into_body(), 4 * 1024).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["decision"], "denied");
    }
}
