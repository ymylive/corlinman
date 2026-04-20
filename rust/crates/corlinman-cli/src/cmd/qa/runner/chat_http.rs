//! `kind: chat_http` handler — drives an in-process `/v1/chat/completions`
//! request through a router built with a [`ScriptedBackend`] that replays
//! the frames declared in the YAML.
//!
//! No real gRPC / no real Python agent. Deterministic enough for CI.

use std::pin::Pin;
use std::sync::Arc;

use async_trait::async_trait;
use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use corlinman_core::CorlinmanError;
use corlinman_gateway::routes::chat::{BackendRx, ChatBackend, ChatState};
use corlinman_gateway::routes::router_with_chat_state;
use corlinman_proto::v1::{
    server_frame, ChatStart, ClientFrame, Done, ErrorInfo, ServerFrame, TokenDelta,
    ToolCall as PbToolCall,
};
use futures::{stream, Stream};
use tokio::sync::mpsc;
use tower::ServiceExt;

use crate::cmd::qa::scenario::{ChatExpect, ChatHttpScenario, FrameScript, JsonContains};

pub async fn run(sc: &ChatHttpScenario) -> anyhow::Result<()> {
    let frames = translate_frames(&sc.frames);
    let backend = Arc::new(ScriptedBackend::new(frames));
    let app: Router = router_with_chat_state(ChatState::new(backend));

    let req = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(build_request_body(sc)?))
        .map_err(|e| anyhow::anyhow!("build request: {e}"))?;

    let resp = app
        .oneshot(req)
        .await
        .map_err(|e| anyhow::anyhow!("oneshot: {e}"))?;

    let status = resp.status();
    let expected = StatusCode::from_u16(sc.expect.status)
        .map_err(|e| anyhow::anyhow!("invalid expected status {}: {e}", sc.expect.status))?;
    if status != expected {
        let body = to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap_or_default();
        anyhow::bail!(
            "status mismatch: expected {expected} got {status} body={}",
            String::from_utf8_lossy(&body)
        );
    }

    // Read whole body; 4 MiB cap is plenty for scripted scenarios.
    let body_bytes = to_bytes(resp.into_body(), 4 << 20)
        .await
        .map_err(|e| anyhow::anyhow!("read body: {e}"))?;
    let body_str = String::from_utf8_lossy(&body_bytes).to_string();

    assert_expectations(&sc.expect, &body_str)?;
    Ok(())
}

fn build_request_body(sc: &ChatHttpScenario) -> anyhow::Result<Vec<u8>> {
    let mut obj = serde_json::Map::new();
    obj.insert("model".into(), sc.request.model.clone().into());
    obj.insert("stream".into(), sc.request.stream.into());
    let msgs: Vec<serde_json::Value> = sc
        .request
        .messages
        .iter()
        .map(|m| {
            serde_json::json!({
                "role": m.role,
                "content": m.content,
            })
        })
        .collect();
    obj.insert("messages".into(), serde_json::Value::Array(msgs));
    if let Some(tools) = &sc.request.tools {
        obj.insert("tools".into(), tools.clone());
    }
    Ok(serde_json::to_vec(&obj)?)
}

fn translate_frames(scripts: &[FrameScript]) -> Vec<ServerFrame> {
    scripts
        .iter()
        .map(|s| match s {
            FrameScript::Token { text } => ServerFrame {
                kind: Some(server_frame::Kind::Token(TokenDelta {
                    text: text.clone(),
                    is_reasoning: false,
                    seq: 0,
                })),
            },
            FrameScript::ToolCall {
                id,
                name,
                arguments,
            } => ServerFrame {
                kind: Some(server_frame::Kind::ToolCall(PbToolCall {
                    call_id: id.clone(),
                    plugin: name.clone(),
                    tool: name.clone(),
                    args_json: serde_json::to_vec(arguments).unwrap_or_default(),
                    seq: 0,
                })),
            },
            FrameScript::Done { reason } => ServerFrame {
                kind: Some(server_frame::Kind::Done(Done {
                    finish_reason: reason.clone(),
                    usage: None,
                    total_tokens_seen: 0,
                    wall_time_ms: 0,
                })),
            },
            FrameScript::Error { message } => ServerFrame {
                kind: Some(server_frame::Kind::Error(ErrorInfo {
                    reason: 0,
                    message: message.clone(),
                    retryable: false,
                    upstream_code: String::new(),
                })),
            },
        })
        .collect()
}

fn assert_expectations(expect: &ChatExpect, body: &str) -> anyhow::Result<()> {
    for frag in &expect.stream_fragments {
        if !body.contains(frag) {
            anyhow::bail!(
                "stream body missing fragment {:?} — body head: {}",
                frag,
                &body.chars().take(200).collect::<String>()
            );
        }
    }
    if !expect.json_contains.is_empty() {
        let json: serde_json::Value = serde_json::from_str(body)
            .map_err(|e| anyhow::anyhow!("parse response JSON: {e}: body={body}"))?;
        for jc in &expect.json_contains {
            assert_json(&json, jc)?;
        }
    }
    Ok(())
}

fn assert_json(root: &serde_json::Value, jc: &JsonContains) -> anyhow::Result<()> {
    let value = follow_path(root, &jc.path)
        .ok_or_else(|| anyhow::anyhow!("json path {:?} missing", jc.path))?;
    if let Some(substr) = &jc.contains {
        let s = value
            .as_str()
            .map(|v| v.to_string())
            .unwrap_or_else(|| value.to_string());
        if !s.contains(substr) {
            anyhow::bail!(
                "json path {:?} expected to contain {:?}, got {}",
                jc.path,
                substr,
                s
            );
        }
    }
    if let Some(expected) = &jc.equals {
        if value != expected {
            anyhow::bail!(
                "json path {:?} expected equals {}, got {}",
                jc.path,
                expected,
                value
            );
        }
    }
    Ok(())
}

/// Follow a dotted path like `choices.0.message.content` through a JSON tree.
/// Numeric segments index into arrays; everything else is a map key.
fn follow_path<'a>(root: &'a serde_json::Value, path: &str) -> Option<&'a serde_json::Value> {
    let mut cur = root;
    for seg in path.split('.') {
        if seg.is_empty() {
            return None;
        }
        match cur {
            serde_json::Value::Object(map) => {
                cur = map.get(seg)?;
            }
            serde_json::Value::Array(arr) => {
                let idx: usize = seg.parse().ok()?;
                cur = arr.get(idx)?;
            }
            _ => return None,
        }
    }
    Some(cur)
}

// ---- Scripted backend -------------------------------------------------------

/// Canned backend that replays a fixed list of [`ServerFrame`]s. Identical in
/// spirit to the `MockBackend` in the gateway's test module, but available
/// to non-test code so the CLI can reuse it.
#[derive(Clone)]
struct ScriptedBackend {
    frames: Arc<tokio::sync::Mutex<Vec<ServerFrame>>>,
}

impl ScriptedBackend {
    fn new(frames: Vec<ServerFrame>) -> Self {
        Self {
            frames: Arc::new(tokio::sync::Mutex::new(frames)),
        }
    }
}

#[async_trait]
impl ChatBackend for ScriptedBackend {
    async fn start(
        &self,
        _start: ChatStart,
    ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
        // We don't inspect client frames in-runner, so drop them into a
        // channel with no consumer — behaviour matches the gateway's
        // test mock.
        let (tx, _rx) = mpsc::channel::<ClientFrame>(16);
        let frames: Vec<_> = std::mem::take(&mut *self.frames.lock().await)
            .into_iter()
            .map(Ok)
            .collect();
        let s: BackendRx = Box::pin(stream::iter(frames))
            as Pin<Box<dyn Stream<Item = Result<ServerFrame, CorlinmanError>> + Send>>;
        Ok((tx, s))
    }
}
