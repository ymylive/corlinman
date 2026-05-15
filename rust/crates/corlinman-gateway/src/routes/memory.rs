//! `/memory/*` — HTTP MemoryHost protocol used by Python curator clients.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use corlinman_memory_host::{
    HealthStatus, LocalSqliteHost, MemoryDoc, MemoryHit, MemoryHost, MemoryQuery,
};
use corlinman_vector::SqliteStore;
use serde::Serialize;
use serde_json::json;

#[derive(Clone)]
pub struct MemoryState {
    host: Arc<dyn MemoryHost>,
}

impl MemoryState {
    pub fn new(host: Arc<dyn MemoryHost>) -> Self {
        Self { host }
    }

    pub fn from_sqlite(store: Arc<SqliteStore>) -> Self {
        Self::new(Arc::new(LocalSqliteHost::new("local-kb", store)))
    }

    pub fn host(&self) -> Arc<dyn MemoryHost> {
        self.host.clone()
    }
}

pub fn router(state: MemoryState) -> Router {
    Router::new()
        .route("/memory/query", post(query))
        .route("/memory/upsert", post(upsert))
        .route("/memory/docs/:id", get(get_doc).delete(delete_doc))
        .route("/memory/health", get(health))
        .with_state(state)
}

#[derive(Debug, Serialize)]
struct QueryResponse {
    hits: Vec<MemoryHit>,
}

#[derive(Debug, Serialize)]
struct UpsertResponse {
    id: String,
}

async fn query(State(state): State<MemoryState>, Json(req): Json<MemoryQuery>) -> Response {
    match state.host.query(req).await {
        Ok(hits) => Json(QueryResponse { hits }).into_response(),
        Err(err) => storage_error(err),
    }
}

async fn upsert(State(state): State<MemoryState>, Json(doc): Json<MemoryDoc>) -> Response {
    match state.host.upsert(doc).await {
        Ok(id) => Json(UpsertResponse { id }).into_response(),
        Err(err) => storage_error(err),
    }
}

async fn get_doc(State(state): State<MemoryState>, Path(id): Path<String>) -> Response {
    match state.host.get(&id).await {
        Ok(Some(hit)) => Json(hit).into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "not_found", "resource": "memory_doc", "id": id})),
        )
            .into_response(),
        Err(err) => storage_error(err),
    }
}

async fn delete_doc(State(state): State<MemoryState>, Path(id): Path<String>) -> Response {
    match state.host.delete(&id).await {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(err) => storage_error(err),
    }
}

async fn health(State(state): State<MemoryState>) -> Response {
    match state.host.health().await {
        HealthStatus::Ok => Json(json!({"status": "ok"})).into_response(),
        HealthStatus::Degraded(msg) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"status": "degraded", "message": msg})),
        )
            .into_response(),
        HealthStatus::Down(msg) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"status": "down", "message": msg})),
        )
            .into_response(),
    }
}

fn storage_error(err: impl std::fmt::Display) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "error": "storage_error",
            "message": err.to_string(),
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::{header, Request, StatusCode};
    use corlinman_vector::migration::ensure_schema;
    use serde_json::json;
    use tempfile::TempDir;
    use tower::ServiceExt;

    async fn app() -> (Router, Arc<SqliteStore>, TempDir) {
        let tmp = TempDir::new().unwrap();
        let store = Arc::new(
            SqliteStore::open(&tmp.path().join("kb.sqlite"))
                .await
                .unwrap(),
        );
        ensure_schema(&store).await.unwrap();
        (router(MemoryState::from_sqlite(store.clone())), store, tmp)
    }

    async fn body_json(resp: Response) -> serde_json::Value {
        let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn upsert_then_query_uses_agent_brain_namespace() {
        let (app, _store, _tmp) = app().await;

        let upsert_body = json!({
            "content": "Project database backend uses PostgreSQL",
            "metadata": {"node_id": "kn-1", "kind": "project_context"},
            "namespace": "agent-brain"
        });
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/memory/upsert")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(upsert_body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let id = body_json(resp).await["id"].as_str().unwrap().to_string();

        let query_body = json!({
            "text": "PostgreSQL backend",
            "top_k": 5,
            "namespace": "agent-brain"
        });
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/memory/query")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(query_body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["hits"][0]["id"], id);
        assert_eq!(v["hits"][0]["metadata"]["namespace"], "agent-brain");
    }

    #[tokio::test]
    async fn query_respects_namespace_filter() {
        let (app, _store, _tmp) = app().await;
        for (content, ns) in [
            ("alpha memory in agent brain", "agent-brain"),
            ("alpha memory in general", "general"),
        ] {
            let body = json!({"content": content, "namespace": ns});
            let resp = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/memory/upsert")
                        .header(header::CONTENT_TYPE, "application/json")
                        .body(Body::from(body.to_string()))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(resp.status(), StatusCode::OK);
        }

        let query_body = json!({
            "text": "alpha memory",
            "top_k": 10,
            "namespace": "agent-brain"
        });
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/memory/query")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(query_body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["hits"].as_array().unwrap().len(), 1);
        assert_eq!(v["hits"][0]["metadata"]["namespace"], "agent-brain");
    }

    #[tokio::test]
    async fn get_and_delete_doc_round_trip() {
        let (app, _store, _tmp) = app().await;
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/memory/upsert")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(
                        json!({"content": "delete me", "namespace": "agent-brain"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let id = body_json(resp).await["id"].as_str().unwrap().to_string();

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!("/memory/docs/{id}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri(format!("/memory/docs/{id}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri(format!("/memory/docs/{id}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }
}
