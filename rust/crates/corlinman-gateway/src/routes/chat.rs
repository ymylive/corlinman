//! `POST /v1/chat/completions` — OpenAI-compatible chat entry point.
//!
//! M1+M2 scope:
//!   * Accept OpenAI-shaped JSON request (model, messages, stream, temperature,
//!     max_tokens, tools).
//!   * Open a bidirectional `Agent.Chat` stream against the Python gRPC server
//!     via [`corlinman_agent_client`].
//!   * Non-streaming: drain every [`ServerFrame`] and assemble an OpenAI-shaped
//!     response. Tool calls surface in `choices[0].message.tool_calls` as the
//!     standard `[{id, type: "function", function: {name, arguments}}]` array.
//!   * Streaming: Server-Sent Events. Each [`TokenDelta`] becomes one SSE
//!     `data:` event with `choices[0].delta.content`; [`ToolCall`] frames
//!     become SSE `choices[0].delta.tool_calls[]` deltas shaped exactly like
//!     OpenAI's streaming tool_calls protocol; the terminal sentinel is
//!     `data: [DONE]`.
//!   * Plan §14 R5 decision: custom markers not allowed; the only tool-call
//!     protocol is OpenAI-standard JSON.
//!   * Tool calls are **parsed but not executed** in M2 — the gateway hands the
//!     placeholder `awaiting_plugin_runtime` payload back to Python as a
//!     [`ToolResult`] so the reasoning loop can terminate; that placeholder is
//!     NOT surfaced in SSE (would confuse OpenAI-compatible clients). Real
//!     plugin execution lands in M3.
//!
//! The handler is kept small by isolating the backend-facing concern behind
//! the [`ChatBackend`] trait. `grpc::GrpcBackend` is the production wiring;
//! tests inject `mock::MockBackend`.

use std::sync::Arc;

use async_trait::async_trait;
use axum::{
    extract::State,
    http::StatusCode,
    response::{
        sse::{Event as SseEvent, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::post,
    Json, Router,
};
use corlinman_agent_client::tool_callback::{PlaceholderExecutor, ToolExecutor};
use corlinman_core::CorlinmanError;
use corlinman_proto::v1::{
    client_frame, server_frame, ChatStart, ClientFrame, Message as PbMessage, Role, ServerFrame,
    ToolCall as PbToolCall,
};
use futures::{stream, Stream};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::convert::Infallible;
use std::pin::Pin;
use tokio::sync::mpsc;
use tracing::{debug, error, warn};
use uuid::Uuid;

// ---- Request / response shapes ------------------------------------------------

/// OpenAI-compatible chat request body (subset — we only consume fields the
/// reasoning loop currently needs).
#[derive(Debug, Clone, Deserialize)]
pub struct ChatRequest {
    pub model: String,
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub stream: bool,
    #[serde(default)]
    pub temperature: Option<f32>,
    #[serde(default)]
    pub max_tokens: Option<u32>,
    /// Opaque `tools` array (OpenAI shape). We pass it through to Python as JSON.
    #[serde(default)]
    pub tools: Option<Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ChatMessage {
    pub role: String,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub tool_call_id: Option<String>,
}

/// OpenAI-shaped non-streaming response.
#[derive(Debug, Serialize)]
pub struct ChatResponse {
    pub id: String,
    pub object: &'static str,
    pub model: String,
    pub choices: Vec<Choice>,
    pub usage: Usage,
}

#[derive(Debug, Serialize)]
pub struct Choice {
    pub index: u32,
    pub message: AssistantMessage,
    pub finish_reason: String,
}

#[derive(Debug, Serialize)]
pub struct AssistantMessage {
    pub role: &'static str,
    pub content: String,
    /// Standard OpenAI `tool_calls` array. Empty → field is omitted.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
}

/// OpenAI-standard tool_call envelope (non-streaming form).
#[derive(Debug, Serialize, Clone)]
pub struct ToolCall {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub function: FunctionCall,
}

#[derive(Debug, Serialize, Clone)]
pub struct FunctionCall {
    pub name: String,
    /// JSON-encoded arguments string (OpenAI sends this verbatim as a string).
    pub arguments: String,
}

#[derive(Debug, Serialize, Default)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
}

// ---- Backend abstraction ------------------------------------------------------

/// Abstraction over the Rust→Python agent bridge so handlers stay testable
/// without booting a real gRPC server.
#[async_trait]
pub trait ChatBackend: Send + Sync {
    /// Open a streaming session. Returns a mpsc-backed receiver for
    /// [`ServerFrame`]s and a sender for [`ClientFrame`]s (so the gateway can
    /// push `ToolResult`/`Cancel`).
    async fn start(
        &self,
        start: ChatStart,
    ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError>;
}

/// Boxed stream of `ServerFrame`s yielded by the backend.
pub type BackendRx = Pin<Box<dyn Stream<Item = Result<ServerFrame, CorlinmanError>> + Send>>;

// ---- Router + handler ---------------------------------------------------------

/// State holder for the chat route: any [`ChatBackend`] impl + the tool executor.
#[derive(Clone)]
pub struct ChatState {
    pub backend: Arc<dyn ChatBackend>,
    pub tool_executor: Arc<dyn ToolExecutor>,
}

impl ChatState {
    /// Bundle a backend with the default M1 placeholder tool executor.
    pub fn new(backend: Arc<dyn ChatBackend>) -> Self {
        Self {
            backend,
            tool_executor: Arc::new(PlaceholderExecutor),
        }
    }
}

/// Default router: 501 Not Implemented. Production callers should build
/// [`router_with_state`] once the gRPC client is available.
pub fn router() -> Router {
    Router::new().route(
        "/v1/chat/completions",
        post(|| async {
            (
                StatusCode::NOT_IMPLEMENTED,
                Json(json!({
                    "error": "not_implemented",
                    "message": "no ChatBackend wired; build router via router_with_state()",
                })),
            )
        }),
    )
}

/// Chat-completions router backed by the supplied [`ChatState`].
pub fn router_with_state(state: ChatState) -> Router {
    Router::new()
        .route("/v1/chat/completions", post(handle_chat))
        .with_state(state)
}

async fn handle_chat(State(state): State<ChatState>, Json(req): Json<ChatRequest>) -> Response {
    if req.model.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "invalid_request",
            "`model` is required",
        );
    }
    if req.messages.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "invalid_request",
            "`messages` must be non-empty",
        );
    }

    let start = build_chat_start(&req);

    if req.stream {
        match chat_stream(state, start, req.model).await {
            Ok(sse) => sse.into_response(),
            Err(err) => upstream_error(err),
        }
    } else {
        match chat_nonstream(state, start, req.model).await {
            Ok(resp) => Json(resp).into_response(),
            Err(err) => upstream_error(err),
        }
    }
}

// ---- Non-streaming ------------------------------------------------------------

async fn chat_nonstream(
    state: ChatState,
    start: ChatStart,
    model: String,
) -> Result<ChatResponse, CorlinmanError> {
    let (tx, mut rx) = state.backend.start(start).await?;

    let mut content = String::new();
    let mut tool_calls: Vec<ToolCall> = Vec::new();
    let mut finish_reason = "stop".to_string();
    let mut usage = Usage::default();

    use futures::StreamExt;
    while let Some(frame) = rx.next().await {
        let frame = frame?;
        match frame.kind {
            Some(server_frame::Kind::Token(t)) => content.push_str(&t.text),
            Some(server_frame::Kind::ToolCall(tc)) => {
                tool_calls.push(pb_tool_call_to_openai(&tc));
                // Ack the call with the M2 placeholder result so the Python
                // side can advance its loop. The placeholder is NOT surfaced
                // to the client — only logged — to avoid confusing an
                // OpenAI-compatible consumer.
                let result = state.tool_executor.execute(&tc).await?;
                debug!(
                    call_id = %tc.call_id,
                    status = "awaiting_plugin_runtime",
                    "gateway.tool_call.placeholder_ack"
                );
                let _ = tx
                    .send(ClientFrame {
                        kind: Some(client_frame::Kind::ToolResult(result)),
                    })
                    .await;
            }
            Some(server_frame::Kind::Done(d)) => {
                finish_reason = normalise_finish_reason(&d.finish_reason, !tool_calls.is_empty());
                if let Some(u) = d.usage {
                    usage.prompt_tokens = u.prompt_tokens;
                    usage.completion_tokens = u.completion_tokens;
                    usage.total_tokens = u.total_tokens;
                }
                break;
            }
            Some(server_frame::Kind::Error(e)) => {
                return Err(CorlinmanError::Upstream {
                    reason: reason_from_proto(e.reason),
                    message: e.message,
                });
            }
            Some(server_frame::Kind::Awaiting(_)) | Some(server_frame::Kind::Usage(_)) | None => {
                // Ignored in M1; approval + streaming usage land with M3.
            }
        }
    }

    Ok(ChatResponse {
        id: format!("chatcmpl-{}", Uuid::new_v4()),
        object: "chat.completion",
        model,
        choices: vec![Choice {
            index: 0,
            message: AssistantMessage {
                role: "assistant",
                content,
                tool_calls,
            },
            finish_reason,
        }],
        usage,
    })
}

// ---- Streaming (SSE) ----------------------------------------------------------

async fn chat_stream(
    state: ChatState,
    start: ChatStart,
    model: String,
) -> Result<Sse<impl Stream<Item = Result<SseEvent, Infallible>>>, CorlinmanError> {
    let (tx, rx) = state.backend.start(start).await?;
    let executor = state.tool_executor.clone();
    let id = format!("chatcmpl-{}", Uuid::new_v4());

    let sse_stream = build_sse_stream(rx, tx, executor, id, model);
    Ok(Sse::new(sse_stream).keep_alive(KeepAlive::default()))
}

fn build_sse_stream(
    rx: BackendRx,
    tx: mpsc::Sender<ClientFrame>,
    executor: Arc<dyn ToolExecutor>,
    id: String,
    model: String,
) -> impl Stream<Item = Result<SseEvent, Infallible>> + Send {
    use futures::StreamExt;

    // State threaded through the stream as it folds.
    struct StreamState {
        rx: BackendRx,
        tx: mpsc::Sender<ClientFrame>,
        executor: Arc<dyn ToolExecutor>,
        id: String,
        model: String,
        /// Next SSE tool_call index; one per distinct `ToolCall` frame seen.
        next_tool_index: u32,
        /// True once a frame triggered termination; suppresses further pulls.
        done: bool,
        /// True once at least one tool_call was surfaced — used to override a
        /// blank `finish_reason` to `"tool_calls"` when appropriate.
        tool_calls_seen: bool,
    }

    let state = StreamState {
        rx,
        tx,
        executor,
        id,
        model,
        next_tool_index: 0,
        done: false,
        tool_calls_seen: false,
    };

    stream::unfold(state, |mut s| async move {
        if s.done {
            return None;
        }
        loop {
            match s.rx.next().await {
                Some(Ok(frame)) => match frame.kind {
                    Some(server_frame::Kind::Token(t)) => {
                        let chunk = token_delta_chunk(&s.id, &s.model, &t.text);
                        let ev = SseEvent::default().data(chunk.to_string());
                        return Some((Ok(ev), s));
                    }
                    Some(server_frame::Kind::ToolCall(tc)) => {
                        let idx = s.next_tool_index;
                        s.next_tool_index += 1;
                        s.tool_calls_seen = true;

                        // Echo placeholder result back so Python isn't stuck.
                        // The placeholder body never reaches the SSE stream.
                        if let Ok(result) = s.executor.execute(&tc).await {
                            debug!(
                                call_id = %tc.call_id,
                                status = "awaiting_plugin_runtime",
                                "gateway.tool_call.placeholder_ack"
                            );
                            let _ =
                                s.tx.send(ClientFrame {
                                    kind: Some(client_frame::Kind::ToolResult(result)),
                                })
                                .await;
                        }

                        let chunk = tool_call_delta_chunk(&s.id, &s.model, idx, &tc);
                        let ev = SseEvent::default().data(chunk.to_string());
                        return Some((Ok(ev), s));
                    }
                    Some(server_frame::Kind::Done(d)) => {
                        let finish = normalise_finish_reason(&d.finish_reason, s.tool_calls_seen);
                        let chunk = finish_chunk(&s.id, &s.model, &finish);
                        s.done = true;
                        let ev = SseEvent::default().data(chunk.to_string());
                        return Some((Ok(ev), s));
                    }
                    Some(server_frame::Kind::Error(e)) => {
                        let chunk = json!({
                            "error": {
                                "code": "upstream_error",
                                "reason": reason_from_proto(e.reason).as_str(),
                                "message": e.message,
                            }
                        });
                        s.done = true;
                        let ev = SseEvent::default().data(chunk.to_string());
                        return Some((Ok(ev), s));
                    }
                    Some(server_frame::Kind::Awaiting(_))
                    | Some(server_frame::Kind::Usage(_))
                    | None => {
                        // Skip and pull the next frame.
                        continue;
                    }
                },
                Some(Err(err)) => {
                    warn!(error = %err, "chat stream backend error");
                    let chunk = json!({
                        "error": {
                            "code": "upstream_error",
                            "message": err.to_string(),
                        }
                    });
                    s.done = true;
                    let ev = SseEvent::default().data(chunk.to_string());
                    return Some((Ok(ev), s));
                }
                None => {
                    return None;
                }
            }
        }
    })
    .chain(stream::once(async {
        Ok::<_, Infallible>(SseEvent::default().data("[DONE]"))
    }))
}

// ---- Helpers ------------------------------------------------------------------

fn build_chat_start(req: &ChatRequest) -> ChatStart {
    let messages = req
        .messages
        .iter()
        .map(|m| PbMessage {
            role: role_from_str(&m.role) as i32,
            content: m.content.clone(),
            name: m.name.clone().unwrap_or_default(),
            tool_call_id: m.tool_call_id.clone().unwrap_or_default(),
            content_json: Default::default(),
        })
        .collect();
    let tools_json = req
        .tools
        .as_ref()
        .and_then(|v| serde_json::to_vec(v).ok())
        .unwrap_or_default();
    ChatStart {
        model: req.model.clone(),
        messages,
        tools_json,
        session_key: String::new(),
        binding: None,
        placeholders: Default::default(),
        temperature: req.temperature.unwrap_or(0.0),
        max_tokens: req.max_tokens.unwrap_or(0),
        stream: req.stream,
        trace: None,
        provider_config_json: Default::default(),
    }
}

fn role_from_str(s: &str) -> Role {
    match s {
        "user" => Role::User,
        "assistant" => Role::Assistant,
        "system" => Role::System,
        "tool" => Role::Tool,
        _ => Role::Unspecified,
    }
}

/// Convert a protobuf `ToolCall` into the OpenAI-standard non-streaming
/// envelope. We forward `args_json` verbatim as the `arguments` string —
/// OpenAI expects stringified JSON, which matches what the Python agent
/// aggregates out of `tool_call_delta` fragments.
fn pb_tool_call_to_openai(tc: &PbToolCall) -> ToolCall {
    let arguments = if tc.args_json.is_empty() {
        "{}".to_string()
    } else {
        String::from_utf8(tc.args_json.clone()).unwrap_or_else(|_| "{}".to_string())
    };
    ToolCall {
        id: tc.call_id.clone(),
        kind: "function",
        function: FunctionCall {
            name: tc.tool.clone(),
            arguments,
        },
    }
}

/// Build the OpenAI streaming `tool_calls[i]` delta chunk. The `index`
/// identifies the slot in the assistant's in-flight tool_calls array so a
/// downstream OpenAI-compatible client can reconstruct the full call.
fn tool_call_delta_chunk(id: &str, model: &str, index: u32, tc: &PbToolCall) -> Value {
    let arguments = if tc.args_json.is_empty() {
        "{}".to_string()
    } else {
        String::from_utf8(tc.args_json.clone()).unwrap_or_else(|_| "{}".to_string())
    };
    json!({
        "id": id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": index,
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.tool,
                        "arguments": arguments,
                    }
                }]
            },
            "finish_reason": null
        }]
    })
}

fn token_delta_chunk(id: &str, model: &str, text: &str) -> Value {
    json!({
        "id": id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": text},
            "finish_reason": null
        }]
    })
}

fn finish_chunk(id: &str, model: &str, finish_reason: &str) -> Value {
    json!({
        "id": id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
    })
}

/// Normalise the Python-side `finish_reason` to the OpenAI-standard set
/// (`"stop" | "length" | "tool_calls" | "error"`). The legacy reason
/// `"tool_call"` (singular) is remapped to `"tool_calls"`; empty strings fall
/// back to `"tool_calls"` if the stream carried any tool calls, else `"stop"`.
fn normalise_finish_reason(raw: &str, had_tool_calls: bool) -> String {
    match raw {
        "stop" | "length" | "tool_calls" | "error" => raw.to_string(),
        "tool_call" => "tool_calls".to_string(),
        "" => {
            if had_tool_calls {
                "tool_calls".into()
            } else {
                "stop".into()
            }
        }
        other => other.to_string(),
    }
}

fn reason_from_proto(code: i32) -> corlinman_core::FailoverReason {
    use corlinman_core::FailoverReason as F;
    match code {
        1 => F::Billing,
        2 => F::RateLimit,
        3 => F::Auth,
        4 => F::AuthPermanent,
        5 => F::Timeout,
        6 => F::ModelNotFound,
        7 => F::Format,
        8 => F::ContextOverflow,
        9 => F::Overloaded,
        10 => F::Unknown,
        _ => F::Unspecified,
    }
}

fn error_response(status: StatusCode, code: &'static str, message: &str) -> Response {
    (
        status,
        Json(json!({
            "error": {"code": code, "message": message}
        })),
    )
        .into_response()
}

fn upstream_error(err: CorlinmanError) -> Response {
    error!(error = %err, "chat route upstream error");
    (
        err.status_code(),
        Json(json!({
            "error": {"code": err.code(), "message": err.to_string()}
        })),
    )
        .into_response()
}

// ---- Production gRPC backend --------------------------------------------------

pub mod grpc {
    //! Real backend backed by `corlinman_agent_client::client::AgentClient`.
    //!
    //! Opens a bidi `Agent.Chat` stream, sends the `ChatStart` frame, then
    //! forwards every `ServerFrame` into an internal mpsc. A spawned task pumps
    //! messages so callers can treat the return as a plain `Stream`.

    use super::*;
    use corlinman_agent_client::client::AgentClient;
    use corlinman_agent_client::retry::status_to_error;
    use tokio::sync::Mutex;
    use tokio_stream::wrappers::ReceiverStream;
    use tonic::Request;

    /// Wraps a pooled [`AgentClient`] so multiple requests can share one channel.
    #[derive(Clone)]
    pub struct GrpcBackend {
        client: Arc<Mutex<AgentClient>>,
    }

    impl GrpcBackend {
        pub fn new(client: AgentClient) -> Self {
            Self {
                client: Arc::new(Mutex::new(client)),
            }
        }
    }

    #[async_trait]
    impl ChatBackend for GrpcBackend {
        async fn start(
            &self,
            start: ChatStart,
        ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
            // IMPORTANT: push ChatStart into the outbound mpsc BEFORE calling
            // `chat()`. grpc.aio (Python) defers sending initial response
            // headers until its handler awaits a request frame, while tonic's
            // `chat().await` blocks until response headers arrive — so if we
            // call `chat()` first and then `tx.send(start)` we deadlock. By
            // pre-loading the sender, tonic's background body-pump delivers
            // the first DATA frame, Python's `_expect_start` unblocks, the
            // handler yields (or awaits again), grpc.aio flushes initial
            // metadata, and tonic's `chat()` resolves.
            let (tx, rx) = mpsc::channel::<ClientFrame>(16);
            tx.send(ClientFrame {
                kind: Some(client_frame::Kind::Start(start)),
            })
            .await
            .map_err(|e| CorlinmanError::Internal(format!("agent channel closed: {e}")))?;
            let outbound = ReceiverStream::new(rx);

            let mut client = self.client.lock().await.clone();
            let response = client
                .inner_mut()
                .chat(Request::new(outbound))
                .await
                .map_err(status_to_error)?;
            let mut rx_stream = response.into_inner();

            // Pump incoming frames into a local channel so the handler sees a
            // plain `Stream` of `Result<ServerFrame, CorlinmanError>`.
            let (out_tx, out_rx) = mpsc::channel::<Result<ServerFrame, CorlinmanError>>(16);
            tokio::spawn(async move {
                loop {
                    match rx_stream.message().await {
                        Ok(Some(frame)) => {
                            if out_tx.send(Ok(frame)).await.is_err() {
                                break;
                            }
                        }
                        Ok(None) => break,
                        Err(status) => {
                            let _ = out_tx.send(Err(status_to_error(status))).await;
                            break;
                        }
                    }
                }
            });
            let stream = ReceiverStream::new(out_rx);
            Ok((tx, Box::pin(stream)))
        }
    }
}

// ---- Tests --------------------------------------------------------------------

#[cfg(test)]
mod mock {
    //! In-memory `ChatBackend` used exclusively by unit tests.

    use super::*;

    /// Scripted server frames returned from `start()`. Frames are consumed on
    /// the first `start()` call; a second call will see an empty stream.
    #[derive(Default, Clone)]
    pub struct MockBackend {
        pub frames: Arc<tokio::sync::Mutex<Vec<ServerFrame>>>,
        pub captured_tx: Arc<tokio::sync::Mutex<Option<mpsc::Sender<ClientFrame>>>>,
        /// When true, `start()` returns a ready-made error — simulates a dead
        /// gRPC peer.
        pub fail_on_start: bool,
    }

    impl MockBackend {
        pub fn with_frames(frames: Vec<ServerFrame>) -> Self {
            Self {
                frames: Arc::new(tokio::sync::Mutex::new(frames)),
                ..Default::default()
            }
        }
    }

    #[async_trait]
    impl ChatBackend for MockBackend {
        async fn start(
            &self,
            _start: ChatStart,
        ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
            if self.fail_on_start {
                return Err(CorlinmanError::Upstream {
                    reason: corlinman_core::FailoverReason::Overloaded,
                    message: "mock backend refused".into(),
                });
            }
            let (tx, _rx) = mpsc::channel::<ClientFrame>(16);
            *self.captured_tx.lock().await = Some(tx.clone());
            let frames: Vec<_> = std::mem::take(&mut *self.frames.lock().await)
                .into_iter()
                .map(Ok)
                .collect();
            let s = stream::iter(frames);
            Ok((tx, Box::pin(s)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::mock::MockBackend;
    use super::*;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use corlinman_proto::v1::{Done, ErrorInfo, TokenDelta};
    use tower::ServiceExt;

    fn app(backend: Arc<dyn ChatBackend>) -> Router {
        router_with_state(ChatState::new(backend))
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

    fn done(reason: &str) -> ServerFrame {
        ServerFrame {
            kind: Some(server_frame::Kind::Done(Done {
                finish_reason: reason.into(),
                usage: None,
                total_tokens_seen: 0,
                wall_time_ms: 0,
            })),
        }
    }

    fn tool_call(call_id: &str, tool: &str, args_json: &str) -> ServerFrame {
        ServerFrame {
            kind: Some(server_frame::Kind::ToolCall(PbToolCall {
                call_id: call_id.into(),
                plugin: tool.into(),
                tool: tool.into(),
                args_json: args_json.as_bytes().to_vec(),
                seq: 0,
            })),
        }
    }

    fn parse_sse_chunks(body_text: &str) -> Vec<Value> {
        body_text
            .lines()
            .filter_map(|line| line.strip_prefix("data: "))
            .filter(|s| *s != "[DONE]")
            .filter_map(|s| serde_json::from_str::<Value>(s).ok())
            .collect()
    }

    #[tokio::test]
    async fn nonstream_happy_path_concatenates_tokens() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            token("hello "),
            token("world"),
            done("stop"),
        ]));
        let app = app(backend);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["choices"][0]["message"]["content"], "hello world");
        assert_eq!(v["choices"][0]["finish_reason"], "stop");
        assert_eq!(v["object"], "chat.completion");
        // No tool_calls field when none were emitted.
        assert!(v["choices"][0]["message"]
            .as_object()
            .unwrap()
            .get("tool_calls")
            .is_none());
    }

    #[tokio::test]
    async fn stream_returns_sse_with_done_sentinel() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            token("ab"),
            token("cd"),
            done("stop"),
        ]));
        let app = app(backend);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": true
                }))
                .unwrap(),
            ))
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let ct = resp
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        assert!(ct.starts_with("text/event-stream"), "ct was: {ct}");
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        assert!(text.contains("\"ab\""), "missing first token chunk: {text}");
        assert!(
            text.contains("\"cd\""),
            "missing second token chunk: {text}"
        );
        assert!(
            text.contains("data: [DONE]"),
            "missing DONE sentinel: {text}"
        );
    }

    #[tokio::test]
    async fn stream_emits_openai_tool_calls_delta() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            token("thinking..."),
            tool_call("call_abc", "FooPlugin", r#"{"q":"hi"}"#),
            done("tool_calls"),
        ]));
        let app = app(backend);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "go"}],
                    "stream": true
                }))
                .unwrap(),
            ))
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();

        // Custom markers must NOT appear (OpenAI-standard only).
        assert!(
            !text.contains("_legacy_tool_call"),
            "non-standard marker leaked: {text}"
        );
        assert!(
            !text.contains("legacy_tool_calls"),
            "non-standard marker leaked: {text}"
        );

        let chunks = parse_sse_chunks(&text);
        // Find the tool_calls delta.
        let tool_chunk = chunks
            .iter()
            .find(|c| c["choices"][0]["delta"].get("tool_calls").is_some())
            .expect("no tool_calls delta emitted");
        let tc = &tool_chunk["choices"][0]["delta"]["tool_calls"][0];
        assert_eq!(tc["index"], 0);
        assert_eq!(tc["id"], "call_abc");
        assert_eq!(tc["type"], "function");
        assert_eq!(tc["function"]["name"], "FooPlugin");
        // `arguments` must be a JSON string that is itself valid JSON.
        let args_str = tc["function"]["arguments"]
            .as_str()
            .expect("arguments must be string");
        let parsed: Value = serde_json::from_str(args_str).unwrap();
        assert_eq!(parsed["q"], "hi");

        // Final finish_reason chunk must be "tool_calls".
        let finish = chunks
            .iter()
            .find(|c| c["choices"][0]["finish_reason"].is_string())
            .expect("no finish chunk");
        assert_eq!(finish["choices"][0]["finish_reason"], "tool_calls");
    }

    #[tokio::test]
    async fn stream_multiple_tool_calls_have_distinct_indices() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            tool_call("c0", "A", r#"{"k":0}"#),
            tool_call("c1", "B", r#"{"k":1}"#),
            done("tool_calls"),
        ]));
        let app = app(backend);
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "go"}],
                    "stream": true
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        let chunks = parse_sse_chunks(&text);

        let tool_deltas: Vec<&Value> = chunks
            .iter()
            .filter(|c| c["choices"][0]["delta"].get("tool_calls").is_some())
            .collect();
        assert_eq!(tool_deltas.len(), 2);
        assert_eq!(
            tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]["index"],
            0
        );
        assert_eq!(
            tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]["id"],
            "c0"
        );
        assert_eq!(
            tool_deltas[1]["choices"][0]["delta"]["tool_calls"][0]["index"],
            1
        );
        assert_eq!(
            tool_deltas[1]["choices"][0]["delta"]["tool_calls"][0]["id"],
            "c1"
        );
    }

    #[tokio::test]
    async fn stream_tool_call_then_text_continues() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            tool_call("c0", "A", r#"{}"#),
            token("follow-up"),
            done("stop"),
        ]));
        let app = app(backend);
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "go"}],
                    "stream": true
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        let chunks = parse_sse_chunks(&text);

        // Must carry a tool_calls delta AND a content delta AND a final stop.
        assert!(chunks
            .iter()
            .any(|c| c["choices"][0]["delta"].get("tool_calls").is_some()));
        assert!(chunks
            .iter()
            .any(|c| c["choices"][0]["delta"]["content"] == "follow-up"));
        let finish = chunks
            .iter()
            .find(|c| c["choices"][0]["finish_reason"].is_string())
            .unwrap();
        assert_eq!(finish["choices"][0]["finish_reason"], "stop");
    }

    #[tokio::test]
    async fn nonstream_surfaces_openai_tool_calls() {
        let backend = Arc::new(MockBackend::with_frames(vec![
            token("prefix "),
            tool_call("call_bar", "BarPlugin", r#"{"x":1}"#),
            done("tool_calls"),
        ]));
        let app = app(backend);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "go"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();

        let resp = app.oneshot(req).await.unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        let tool_calls = &v["choices"][0]["message"]["tool_calls"];
        assert!(tool_calls.is_array(), "expected tool_calls array, got: {v}");
        let first = &tool_calls[0];
        assert_eq!(first["id"], "call_bar");
        assert_eq!(first["type"], "function");
        assert_eq!(first["function"]["name"], "BarPlugin");
        // arguments is a JSON string.
        let args_str = first["function"]["arguments"].as_str().unwrap();
        let parsed: Value = serde_json::from_str(args_str).unwrap();
        assert_eq!(parsed["x"], 1);
        assert_eq!(v["choices"][0]["finish_reason"], "tool_calls");
        // Non-standard markers not allowed — only OpenAI `tool_calls` key.
        assert!(v["choices"][0]["message"]
            .as_object()
            .unwrap()
            .get("legacy_tool_calls")
            .is_none());
    }

    #[tokio::test]
    async fn invalid_request_rejects_empty_messages() {
        let backend = Arc::new(MockBackend::default());
        let app = app(backend);
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": []
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn upstream_error_frame_surfaces_in_nonstream() {
        let err_frame = ServerFrame {
            kind: Some(server_frame::Kind::Error(ErrorInfo {
                reason: 9, // OVERLOADED
                message: "provider overloaded".into(),
                retryable: true,
                upstream_code: "anthropic.overloaded_error".into(),
            })),
        };
        let backend = Arc::new(MockBackend::with_frames(vec![err_frame]));
        let app = app(backend);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}]
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        // OVERLOADED maps to 503.
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn backend_start_failure_returns_error_status() {
        let backend = Arc::new(MockBackend {
            fail_on_start: true,
            ..Default::default()
        });
        let app = app(backend);
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "hi"}]
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn finish_reason_normalises_legacy_tool_call_variant() {
        // Some providers emit "tool_call" (singular); the gateway must
        // normalise to the OpenAI-standard "tool_calls".
        let backend = Arc::new(MockBackend::with_frames(vec![
            tool_call("c0", "T", r#"{}"#),
            done("tool_call"),
        ]));
        let app = app(backend);
        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "go"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["choices"][0]["finish_reason"], "tool_calls");
    }
}
