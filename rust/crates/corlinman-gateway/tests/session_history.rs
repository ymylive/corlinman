//! Session history persistence integration tests — roadmap §3 S1.T4.
//!
//! Covers the cross-request round-trip:
//!
//!   1. First `POST /v1/chat/completions` with `session_key="s1"` produces a
//!      user + assistant row in the on-disk SQLite store.
//!   2. Second request with the same key hands the backend a `ChatStart` that
//!      includes the previous assistant message prepended ahead of the new
//!      user turn.
//!
//! The scripted `ChatBackend` captures the `ChatStart` so the assertion can
//! inspect the exact message list the gateway would forward to Python.

use std::pin::Pin;
use std::sync::Arc;

use async_trait::async_trait;
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use corlinman_core::session::SessionStore;
use corlinman_core::{CorlinmanError, SqliteSessionStore};
use corlinman_gateway::routes::chat::{BackendRx, ChatBackend, ChatState};
use corlinman_gateway::routes::router_with_chat_state;
use corlinman_proto::v1::{server_frame, ChatStart, ClientFrame, Done, ServerFrame, TokenDelta};
use futures::{stream, Stream};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tower::ServiceExt;

/// Scripted backend that records every `ChatStart` it observes and replays a
/// caller-supplied sequence of `ServerFrame`s per `start()` call.
#[derive(Clone)]
struct CapturingBackend {
    /// Per-call scripted response sequences. One `Vec<ServerFrame>` is
    /// consumed per invocation of `start()`; additional calls past the last
    /// scripted sequence see an empty stream.
    responses: Arc<tokio::sync::Mutex<Vec<Vec<ServerFrame>>>>,
    /// Every `ChatStart` seen so far, in call order. The integration assertion
    /// inspects this to verify history was prepended correctly.
    seen_starts: Arc<tokio::sync::Mutex<Vec<ChatStart>>>,
}

impl CapturingBackend {
    fn new(responses: Vec<Vec<ServerFrame>>) -> Self {
        Self {
            responses: Arc::new(tokio::sync::Mutex::new(responses)),
            seen_starts: Arc::new(tokio::sync::Mutex::new(Vec::new())),
        }
    }

    async fn starts(&self) -> Vec<ChatStart> {
        self.seen_starts.lock().await.clone()
    }
}

#[async_trait]
impl ChatBackend for CapturingBackend {
    async fn start(
        &self,
        start: ChatStart,
    ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
        self.seen_starts.lock().await.push(start);
        let frames = {
            let mut resp = self.responses.lock().await;
            if resp.is_empty() {
                Vec::new()
            } else {
                resp.remove(0)
            }
        };
        let (tx, _rx) = mpsc::channel::<ClientFrame>(16);
        let out: BackendRx = Box::pin(stream::iter(frames.into_iter().map(Ok)))
            as Pin<Box<dyn Stream<Item = Result<ServerFrame, CorlinmanError>> + Send>>;
        Ok((tx, out))
    }
}

fn token(text: &str) -> ServerFrame {
    ServerFrame {
        kind: Some(server_frame::Kind::Token(TokenDelta {
            text: text.into(),
            is_reasoning: false,
            seq: 0,
        })),
    }
}

fn done_stop() -> ServerFrame {
    ServerFrame {
        kind: Some(server_frame::Kind::Done(Done {
            finish_reason: "stop".into(),
            usage: None,
            total_tokens_seen: 0,
            wall_time_ms: 0,
        })),
    }
}

/// Build a fresh on-disk SQLite session store + router wired to the scripted
/// backend. Returns the temp dir so it outlives the test.
async fn fixture(
    responses: Vec<Vec<ServerFrame>>,
) -> (
    Arc<CapturingBackend>,
    Arc<dyn SessionStore>,
    tempfile::TempDir,
    axum::Router,
) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let path = tmp.path().join("sessions.sqlite");
    let store = Arc::new(
        SqliteSessionStore::open(&path)
            .await
            .expect("open sqlite session store"),
    ) as Arc<dyn SessionStore>;

    let backend = Arc::new(CapturingBackend::new(responses));

    let state = ChatState::new(backend.clone() as Arc<dyn ChatBackend>)
        .with_session_store(store.clone())
        .with_session_max_messages(100);
    let app = router_with_chat_state(state);
    (backend, store, tmp, app)
}

fn request_body(session_key: &str, user_content: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "model": "test-model",
        "messages": [{"role": "user", "content": user_content}],
        "stream": false,
        "session_key": session_key,
    }))
    .unwrap()
}

#[tokio::test]
async fn second_request_sees_previous_assistant_message_prepended() {
    // First reply: "I'm an AI". Second reply: "I said I am an AI.".
    let responses = vec![
        vec![token("I'm an AI"), done_stop()],
        vec![token("I said I am an AI."), done_stop()],
    ];
    let (backend, store, _tmp, app) = fixture(responses).await;

    // ---- Turn 1 ---------------------------------------------------------
    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(request_body("s1", "hello")))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["choices"][0]["message"]["content"], "I'm an AI");

    // Store must now contain exactly [user, assistant].
    let history = store.load("s1").await.unwrap();
    assert_eq!(
        history.len(),
        2,
        "expected user+assistant persisted: {history:?}"
    );
    assert_eq!(history[0].content, "hello");
    assert_eq!(history[1].content, "I'm an AI");

    // ---- Turn 2 ---------------------------------------------------------
    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(request_body("s1", "what did you say?")))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // The backend's second ChatStart must contain the prior user+assistant
    // ahead of the new user turn — that's the whole point of T4.
    let starts = backend.starts().await;
    assert_eq!(starts.len(), 2);
    let second = &starts[1];
    assert_eq!(
        second.messages.len(),
        3,
        "expected [user_t1, assistant_t1, user_t2], got {:?}",
        second
            .messages
            .iter()
            .map(|m| (m.role, m.content.clone()))
            .collect::<Vec<_>>()
    );
    assert_eq!(second.messages[0].content, "hello");
    assert_eq!(second.messages[1].content, "I'm an AI");
    assert_eq!(second.messages[2].content, "what did you say?");
    // session_key propagated into the frame so downstream Python sees it.
    assert_eq!(second.session_key, "s1");

    // Store now holds 4 messages total after the second turn.
    let history = store.load("s1").await.unwrap();
    assert_eq!(history.len(), 4);
    assert_eq!(history[2].content, "what did you say?");
    assert_eq!(history[3].content, "I said I am an AI.");
}

#[tokio::test]
async fn missing_session_key_skips_history_persistence() {
    let responses = vec![vec![token("no state for you"), done_stop()]];
    let (backend, store, _tmp, app) = fixture(responses).await;

    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(
            serde_json::to_vec(&json!({
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": false
            }))
            .unwrap(),
        ))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    // Backend saw the ChatStart but with empty session_key → nothing stored.
    let starts = backend.starts().await;
    assert_eq!(starts.len(), 1);
    assert_eq!(starts[0].session_key, "");
    assert!(store.load("anything").await.unwrap().is_empty());
}

#[tokio::test]
async fn session_key_via_header_is_honoured() {
    // Same round-trip as the first test but supplies the key via header rather
    // than body — ensures the `X-Session-Key` fallback actually threads through.
    let responses = vec![
        vec![token("header works"), done_stop()],
        vec![token("second via header"), done_stop()],
    ];
    let (backend, store, _tmp, app) = fixture(responses).await;

    // Turn 1 — no body session_key, only the header.
    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("x-session-key", "hdr-session")
        .body(Body::from(
            serde_json::to_vec(&json!({
                "model": "test-model",
                "messages": [{"role": "user", "content": "first"}],
                "stream": false
            }))
            .unwrap(),
        ))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let hist = store.load("hdr-session").await.unwrap();
    assert_eq!(hist.len(), 2);

    // Turn 2 — same header, history must round-trip.
    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("x-session-key", "hdr-session")
        .body(Body::from(
            serde_json::to_vec(&json!({
                "model": "test-model",
                "messages": [{"role": "user", "content": "second"}],
                "stream": false
            }))
            .unwrap(),
        ))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let starts = backend.starts().await;
    let second = &starts[1];
    assert_eq!(second.messages.len(), 3);
    assert_eq!(second.messages[1].content, "header works");
    assert_eq!(second.session_key, "hdr-session");
}
