//! `/admin/approvals*` — tool-approval queue admin endpoints.
//!
//! Three routes live here (all behind the Basic-auth guard mounted in
//! [`super::router_with_state`]):
//!
//! - `GET /admin/approvals?include_decided=false` — JSON list from
//!   `pending_approvals` via
//!   [`corlinman_vector::SqliteStore::list_pending_approvals`].
//! - `POST /admin/approvals/:id/decide` — body
//!   `{"approve": bool, "reason": "..."}`. Resolves the parked
//!   `check()` oneshot by calling
//!   [`crate::middleware::approval::ApprovalGate::resolve`] and writes
//!   the outcome to SQLite.
//! - `GET /admin/approvals/stream` — Server-Sent Events fed from
//!   [`crate::middleware::approval::ApprovalGate::subscribe`]. Each
//!   event is a single `data: {...}\n\n` JSON frame; SSE's default
//!   `"message"` event name is used so the `EventSource` browser API
//!   sees them without a custom `addEventListener`.
//!
//! When the gateway boots without an `ApprovalGate` attached (no rules
//! in config, or running in test mode with `AdminState::new`), every
//! route here returns 503 `approvals_disabled`.

use std::convert::Infallible;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{
        sse::{Event as SseEvent, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::{get, post},
    Json, Router,
};
use corlinman_vector::PendingApproval;
use futures::Stream;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt;
use tracing::warn;

use super::AdminState;
use crate::middleware::approval::{ApprovalDecision, ApprovalEvent};

/// Sub-router for `/admin/approvals*`. Mounted by
/// [`super::router_with_state`] inside the admin_auth middleware.
pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/admin/approvals", get(list_approvals))
        .route("/admin/approvals/stream", get(stream_approvals))
        .route("/admin/approvals/:id/decide", post(decide_approval))
        .with_state(state)
}

/// `?include_decided=true` flips the list view to show history.
#[derive(Debug, Deserialize, Default)]
pub struct ListQuery {
    #[serde(default)]
    pub include_decided: bool,
}

/// Flat JSON shape returned to the UI. Mirrors `ApprovalItem` on the TS
/// side so the admin page can consume it without an extra adapter.
#[derive(Debug, Serialize)]
pub struct ApprovalOut {
    pub id: String,
    pub plugin: String,
    pub tool: String,
    pub session_key: String,
    pub args_json: String,
    pub requested_at: String,
    pub decided_at: Option<String>,
    pub decision: Option<String>,
}

impl From<PendingApproval> for ApprovalOut {
    fn from(row: PendingApproval) -> Self {
        Self {
            id: row.id,
            plugin: row.plugin,
            tool: row.tool,
            session_key: row.session_key,
            args_json: row.args_json,
            requested_at: row.requested_at,
            decided_at: row.decided_at,
            decision: row.decision,
        }
    }
}

fn approvals_disabled() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": "approvals_disabled",
            "message": "approval gate is not configured on this gateway",
        })),
    )
        .into_response()
}

async fn list_approvals(State(state): State<AdminState>, Query(q): Query<ListQuery>) -> Response {
    let Some(gate) = state.approval_gate.as_ref() else {
        return approvals_disabled();
    };
    match gate
        .store_ref()
        .list_pending_approvals(q.include_decided)
        .await
    {
        Ok(rows) => {
            let out: Vec<ApprovalOut> = rows.into_iter().map(Into::into).collect();
            Json(out).into_response()
        }
        Err(err) => {
            warn!(error = %err, "admin/approvals list failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "storage_error",
                    "message": err.to_string(),
                })),
            )
                .into_response()
        }
    }
}

/// Body shape for `POST /admin/approvals/:id/decide`.
#[derive(Debug, Deserialize)]
pub struct DecideBody {
    pub approve: bool,
    #[serde(default)]
    pub reason: Option<String>,
}

async fn decide_approval(
    State(state): State<AdminState>,
    Path(id): Path<String>,
    Json(body): Json<DecideBody>,
) -> Response {
    let Some(gate) = state.approval_gate.as_ref() else {
        return approvals_disabled();
    };
    let decision = if body.approve {
        ApprovalDecision::Approved
    } else {
        ApprovalDecision::Denied(body.reason.unwrap_or_default())
    };
    match gate.resolve(&id, decision.clone()).await {
        Ok(()) => Json(json!({
            "id": id,
            "decision": decision.db_label(),
        }))
        .into_response(),
        Err(corlinman_core::CorlinmanError::NotFound { .. }) => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "not_found",
                "resource": "approval",
                "id": id,
            })),
        )
            .into_response(),
        Err(err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "decide_failed",
                "message": err.to_string(),
            })),
        )
            .into_response(),
    }
}

async fn stream_approvals(State(state): State<AdminState>) -> Response {
    let Some(gate) = state.approval_gate.as_ref() else {
        return approvals_disabled();
    };
    let rx = gate.subscribe();
    let sse_stream = broadcast_to_sse(rx);
    Sse::new(sse_stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

fn broadcast_to_sse(
    rx: tokio::sync::broadcast::Receiver<ApprovalEvent>,
) -> impl Stream<Item = Result<SseEvent, Infallible>> {
    BroadcastStream::new(rx).filter_map(|item| {
        match item {
            Ok(evt) => {
                let payload = serialize_event(&evt);
                Some(Ok(SseEvent::default().data(payload.to_string())))
            }
            // Laggy subscriber dropped older frames — emit a visible warning
            // rather than tear the stream down; the UI's next poll of
            // `/admin/approvals` will resync ground truth.
            Err(err) => Some(Ok(SseEvent::default().event("lag").data(err.to_string()))),
        }
    })
}

fn serialize_event(evt: &ApprovalEvent) -> serde_json::Value {
    match evt {
        ApprovalEvent::Pending(row) => json!({
            "kind": "pending",
            "approval": ApprovalOut::from(row.clone()),
        }),
        ApprovalEvent::Decided { id, decision } => {
            let reason = match decision {
                ApprovalDecision::Denied(r) => Some(r.as_str()),
                _ => None,
            };
            json!({
                "kind": "decided",
                "id": id,
                "decision": decision.db_label(),
                "reason": reason,
            })
        }
    }
}

// ---------------------------------------------------------------------------
// Gate field accessor helper.
//
// The `/admin/approvals` routes need read access to the underlying SQLite
// store for the list view. The gate owns that store; expose it via a
// narrow accessor so handlers don't reach into private fields.
// ---------------------------------------------------------------------------

impl crate::middleware::approval::ApprovalGate {
    /// Borrow the backing store. Crate-private because the admin layer is
    /// the only caller today; external consumers should go through the
    /// gate's higher-level APIs.
    pub(crate) fn store_ref(&self) -> std::sync::Arc<corlinman_vector::SqliteStore> {
        self.store_arc()
    }
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
    use corlinman_core::config::{ApprovalMode, ApprovalRule, Config};
    use corlinman_plugins::registry::PluginRegistry;
    use corlinman_vector::SqliteStore;
    use std::sync::Arc;
    use std::time::Duration;
    use tempfile::TempDir;
    use tokio_util::sync::CancellationToken;
    use tower::ServiceExt;

    async fn build_gate(
        rules: Vec<ApprovalRule>,
        timeout: Duration,
    ) -> (Arc<ApprovalGate>, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap();
        corlinman_vector::migration::ensure_schema(&store)
            .await
            .unwrap();
        let gate = ApprovalGate::new(rules, Arc::new(store), timeout);
        (Arc::new(gate), tmp)
    }

    fn app_with_gate(gate: Option<Arc<ApprovalGate>>) -> Router {
        let state = AdminState {
            plugins: Arc::new(PluginRegistry::default()),
            config: Arc::new(ArcSwap::from_pointee(Config::default())),
            approval_gate: gate,
            session_store: None,
            config_path: None,
            log_broadcast: None,
            rag_store: None,
            scheduler_history: None,
            py_config_path: None,
            config_watcher: None,
            evolution_store: None,
            evolution_applier: None,
            history_repo: None,
            proposals_repo: None,
            tenant_pool: None,
            allowed_tenants: std::collections::BTreeSet::new(),
            admin_db: None,
            sessions_disabled: false,
            data_dir: None,
            identity_store: None,
            replay_chat_service: None,
        };
        router(state)
    }

    fn rule(plugin: &str, mode: ApprovalMode) -> ApprovalRule {
        ApprovalRule {
            plugin: plugin.into(),
            tool: None,
            mode,
            allow_session_keys: Vec::new(),
        }
    }

    #[tokio::test]
    async fn list_returns_503_when_gate_missing() {
        let app = app_with_gate(None);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/approvals")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn list_returns_pending_rows_from_store() {
        let (gate, _tmp) = build_gate(
            vec![rule("shell", ApprovalMode::Prompt)],
            Duration::from_secs(5),
        )
        .await;
        // Kick off a check that will persist a pending row.
        let g = gate.clone();
        let handle = tokio::spawn(async move {
            let _ = g
                .check("s1", "shell", "exec", b"{}", CancellationToken::new())
                .await;
        });
        // Poll until the row lands.
        for _ in 0..50 {
            if !gate
                .store_ref()
                .list_pending_approvals(false)
                .await
                .unwrap()
                .is_empty()
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }

        let app = app_with_gate(Some(gate.clone()));
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/approvals")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let arr = v.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["plugin"], "shell");
        assert_eq!(arr[0]["tool"], "exec");
        assert!(arr[0]["decided_at"].is_null());

        // Let the hung check drain so the test doesn't leak a task.
        let id = arr[0]["id"].as_str().unwrap().to_string();
        gate.resolve(&id, ApprovalDecision::Approved).await.unwrap();
        let _ = handle.await;
    }

    #[tokio::test]
    async fn decide_approve_wakes_check_and_returns_200() {
        let (gate, _tmp) = build_gate(
            vec![rule("shell", ApprovalMode::Prompt)],
            Duration::from_secs(5),
        )
        .await;
        let g = gate.clone();
        let handle = tokio::spawn(async move {
            g.check("s1", "shell", "exec", b"{}", CancellationToken::new())
                .await
        });
        // Wait for the row.
        let id = loop {
            let rows = gate
                .store_ref()
                .list_pending_approvals(false)
                .await
                .unwrap();
            if let Some(r) = rows.first() {
                break r.id.clone();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        };

        let app = app_with_gate(Some(gate.clone()));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/admin/approvals/{id}/decide"))
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"approve":true}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let decision = handle.await.unwrap().unwrap();
        assert_eq!(decision, ApprovalDecision::Approved);
    }

    #[tokio::test]
    async fn decide_returns_404_for_unknown_id() {
        let (gate, _tmp) = build_gate(Vec::new(), Duration::from_secs(1)).await;
        let app = app_with_gate(Some(gate));
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/approvals/nope/decide")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"approve":false,"reason":"no"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }
}
