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
    http::{HeaderMap, StatusCode},
    response::{
        sse::{Event as SseEvent, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::post,
    Json, Router,
};
use corlinman_agent_client::tool_callback::{PlaceholderExecutor, ToolExecutor};
use corlinman_core::session::{SessionMessage, SessionRole, SessionStore};
use corlinman_core::CorlinmanError;
use corlinman_plugins::runtime::jsonrpc_stdio::{execute as stdio_execute, DEFAULT_TIMEOUT_MS};
use corlinman_plugins::runtime::service_grpc::ServiceRuntime;
use corlinman_plugins::runtime::{PluginInput, PluginOutput};
use corlinman_plugins::{PluginRegistry, PluginType};
use corlinman_proto::v1::{
    client_frame, server_frame, ChatStart, ClientFrame, Message as PbMessage, Role, ServerFrame,
    ToolCall as PbToolCall, ToolResult as PbToolResult,
};
use futures::{stream, Stream};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::convert::Infallible;
use std::pin::Pin;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
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
    /// Optional session identifier. When present, the gateway loads prior
    /// history for this key and persists the new user + assistant messages on
    /// completion. Clients may also supply this via the `X-Session-Key`
    /// header; the body takes precedence. Absent = stateless single-turn.
    #[serde(default)]
    pub session_key: Option<String>,
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

// ---- Model redirect -----------------------------------------------------------

/// Lightweight slice of `config.models` the chat handler needs to resolve a
/// request's `model` field. We keep this detached from `CorlinmanConfig` so
/// `ChatState` doesn't depend on the full config graph (and so tests can
/// script remap behaviour without building a Config tree).
///
/// Resolution order per roadmap §3 T3:
///   1. If `model` is a key in `aliases`, rewrite to `aliases[model]`.
///   2. Else if `known_models` is non-empty and `model` is absent from it,
///      fall back to `default` (when set) and log a warning.
///   3. Else return the model verbatim (stateless pass-through).
#[derive(Debug, Clone, Default)]
pub struct ModelRedirect {
    /// Alias map: request model → resolved model.
    pub aliases: std::collections::HashMap<String, String>,
    /// Fallback target when the request model is unknown. Empty = no fallback.
    pub default: String,
    /// Known models (typically provider `supports` union). Empty = skip the
    /// fallback check entirely (treat every model as known).
    pub known_models: std::collections::HashSet<String>,
}

impl ModelRedirect {
    /// Convenience constructor for tests / boot code.
    pub fn new(
        aliases: std::collections::HashMap<String, String>,
        default: String,
        known_models: std::collections::HashSet<String>,
    ) -> Self {
        Self {
            aliases,
            default,
            known_models,
        }
    }
}

/// Resolution outcome — separated from [`apply_model_aliases`] so the handler
/// can tell "unknown + no default" (→ 400) from the happy paths.
#[derive(Debug, PartialEq, Eq)]
pub enum ResolvedModel {
    /// Alias hit → `resolved` is the aliased target.
    Aliased { resolved: String },
    /// Model is known verbatim (or known-models list is empty / disabled).
    Passthrough { resolved: String },
    /// Model is unknown and a non-empty default was applied.
    FallbackDefault { resolved: String },
    /// Model is unknown and no default is configured — handler returns 400.
    UnknownNoDefault,
}

impl ResolvedModel {
    pub fn model(&self) -> Option<&str> {
        match self {
            Self::Aliased { resolved }
            | Self::Passthrough { resolved }
            | Self::FallbackDefault { resolved } => Some(resolved.as_str()),
            Self::UnknownNoDefault => None,
        }
    }
}

/// Apply the alias map + unknown-model fallback to a request model string.
/// Pure function — no logging, no I/O — so the handler can log once with the
/// right level after inspecting the outcome.
pub fn apply_model_aliases(model: &str, redirect: &ModelRedirect) -> ResolvedModel {
    if let Some(target) = redirect.aliases.get(model) {
        return ResolvedModel::Aliased {
            resolved: target.clone(),
        };
    }
    // Empty known_models = caller opted out of the unknown-model check.
    if redirect.known_models.is_empty() || redirect.known_models.contains(model) {
        return ResolvedModel::Passthrough {
            resolved: model.to_string(),
        };
    }
    if !redirect.default.is_empty() {
        return ResolvedModel::FallbackDefault {
            resolved: redirect.default.clone(),
        };
    }
    ResolvedModel::UnknownNoDefault
}

// ---- Router + handler ---------------------------------------------------------

/// State holder for the chat route: any [`ChatBackend`] impl + the tool executor.
#[derive(Clone)]
pub struct ChatState {
    pub backend: Arc<dyn ChatBackend>,
    pub tool_executor: Arc<dyn ToolExecutor>,
    /// Cross-request session history store. `None` = stateless (no history
    /// load / append / trim). Wired from `AppState::session_store`.
    pub session_store: Option<Arc<dyn SessionStore>>,
    /// Maximum messages retained per session after each turn; older ones are
    /// trimmed asynchronously post-response. Mirrors
    /// `config.server.session_max_messages`.
    pub session_max_messages: usize,
    /// Model alias + fallback configuration. Default = pass-through for every
    /// model (empty aliases + empty known_models).
    pub model_redirect: Arc<ModelRedirect>,
    /// Optional tool-approval gate (Sprint 2 T3). When present, every tool
    /// call is wrapped in an [`ApprovalToolExecutor`] for the duration of
    /// the request, so `Denied` / `Timeout` short-circuit to structured
    /// error results instead of executing the plugin.
    pub approval_gate: Option<Arc<crate::middleware::approval::ApprovalGate>>,
}

/// Default cap used when [`ChatState`] is constructed without an explicit
/// `session_max_messages`. Matches `ServerConfig::default().session_max_messages`.
pub const DEFAULT_SESSION_MAX_MESSAGES: usize = 100;

impl ChatState {
    /// Bundle a backend with the default M1 placeholder tool executor.
    ///
    /// Used by tests and by the boot path when no plugin registry is
    /// available. Production callers should prefer [`ChatState::with_registry`]
    /// so real plugins execute instead of returning the
    /// `awaiting_plugin_runtime` placeholder.
    pub fn new(backend: Arc<dyn ChatBackend>) -> Self {
        Self {
            backend,
            tool_executor: Arc::new(PlaceholderExecutor),
            session_store: None,
            session_max_messages: DEFAULT_SESSION_MAX_MESSAGES,
            model_redirect: Arc::new(ModelRedirect::default()),
            approval_gate: None,
        }
    }

    /// Bundle a backend with a real plugin-registry-backed tool executor.
    /// Tool calls routed through the chat pipeline dispatch to the matching
    /// manifest's JSON-RPC stdio runtime.
    pub fn with_registry(backend: Arc<dyn ChatBackend>, registry: Arc<PluginRegistry>) -> Self {
        Self {
            backend,
            tool_executor: Arc::new(RegistryToolExecutor::new(registry)),
            session_store: None,
            session_max_messages: DEFAULT_SESSION_MAX_MESSAGES,
            model_redirect: Arc::new(ModelRedirect::default()),
            approval_gate: None,
        }
    }

    /// Like [`Self::with_registry`] but also attaches a long-lived gRPC
    /// runtime so `plugin_type = "service"` manifests dispatch through the
    /// supervisor-managed child processes. Used by the gateway boot path.
    pub fn with_registry_and_service_runtime(
        backend: Arc<dyn ChatBackend>,
        registry: Arc<PluginRegistry>,
        service_runtime: Arc<ServiceRuntime>,
    ) -> Self {
        let exec = RegistryToolExecutor::new(registry).with_service_runtime(service_runtime);
        Self {
            backend,
            tool_executor: Arc::new(exec),
            session_store: None,
            session_max_messages: DEFAULT_SESSION_MAX_MESSAGES,
            model_redirect: Arc::new(ModelRedirect::default()),
            approval_gate: None,
        }
    }

    /// Attach a session store so subsequent requests with a `session_key` load
    /// prior history and persist their new user+assistant turns.
    pub fn with_session_store(mut self, store: Arc<dyn SessionStore>) -> Self {
        self.session_store = Some(store);
        self
    }

    /// Override the session message cap (default 100).
    pub fn with_session_max_messages(mut self, max: usize) -> Self {
        self.session_max_messages = max.max(1);
        self
    }

    /// Escape hatch for tests / alternate composition (e.g. custom executors).
    pub fn with_executor(
        backend: Arc<dyn ChatBackend>,
        tool_executor: Arc<dyn ToolExecutor>,
    ) -> Self {
        Self {
            backend,
            tool_executor,
            session_store: None,
            session_max_messages: DEFAULT_SESSION_MAX_MESSAGES,
            model_redirect: Arc::new(ModelRedirect::default()),
            approval_gate: None,
        }
    }

    /// Attach a model-redirect bundle so `/v1/chat/completions` rewrites the
    /// request `model` field through `config.models.aliases` + optional
    /// unknown-model fallback to `config.models.default`.
    pub fn with_model_redirect(mut self, redirect: ModelRedirect) -> Self {
        self.model_redirect = Arc::new(redirect);
        self
    }

    /// Attach a tool-approval gate. Every tool call during the request
    /// is wrapped by an [`ApprovalToolExecutor`] that consults the gate
    /// before delegating to the underlying executor.
    pub fn with_approval_gate(
        mut self,
        gate: Arc<crate::middleware::approval::ApprovalGate>,
    ) -> Self {
        self.approval_gate = Some(gate);
        self
    }

    /// Build the per-request executor: if an approval gate is attached,
    /// wrap the base executor with one scoped to `session_key`; otherwise
    /// return the underlying executor unchanged.
    pub(crate) fn request_executor(&self, session_key: Option<&str>) -> Arc<dyn ToolExecutor> {
        match &self.approval_gate {
            Some(gate) => Arc::new(ApprovalToolExecutor::new(
                self.tool_executor.clone(),
                gate.clone(),
                session_key.unwrap_or("").to_string(),
            )),
            None => self.tool_executor.clone(),
        }
    }
}

// ---- Registry-backed tool executor -------------------------------------------

/// Bridges a `ServerFrame::ToolCall` to the real plugin runtime via
/// [`PluginRegistry`] + [`stdio_execute`], returning a populated
/// [`PbToolResult`] so the Python reasoning loop can continue.
///
/// Errors, timeouts, and not-found manifests are folded into `is_error=true`
/// payloads so the chat stream never aborts on a single bad tool call — the
/// model sees a structured error and can recover on the next round.
pub struct RegistryToolExecutor {
    registry: Arc<PluginRegistry>,
    /// Global fallback deadline when neither manifest nor request specifies one.
    #[allow(dead_code)]
    timeout_default: std::time::Duration,
    /// Deadline for async plugin callbacks. Tests override this via
    /// [`Self::with_async_timeout`] to avoid 5-minute waits.
    async_callback_timeout: std::time::Duration,
    /// Long-lived gRPC runtime used for `plugin_type = "service"` manifests.
    /// Absent in tests that never exercise service plugins; an executor with
    /// `None` here returns `plugin_runtime` errors for service calls.
    service_runtime: Option<Arc<ServiceRuntime>>,
}

/// Default deadline we wait for a `/plugin-callback/:task_id` HTTP hit
/// before giving up on an async tool call. Roadmap §3 specifies 5 minutes.
pub const DEFAULT_ASYNC_CALLBACK_TIMEOUT_SECS: u64 = 300;

impl RegistryToolExecutor {
    pub fn new(registry: Arc<PluginRegistry>) -> Self {
        Self {
            registry,
            timeout_default: std::time::Duration::from_millis(DEFAULT_TIMEOUT_MS),
            async_callback_timeout: std::time::Duration::from_secs(
                DEFAULT_ASYNC_CALLBACK_TIMEOUT_SECS,
            ),
            service_runtime: None,
        }
    }

    /// Attach a long-lived gRPC runtime so service-type plugins dispatch
    /// through the supervised child processes instead of falling back to
    /// a `plugin_runtime` error.
    pub fn with_service_runtime(mut self, runtime: Arc<ServiceRuntime>) -> Self {
        self.service_runtime = Some(runtime);
        self
    }

    /// Override the async callback timeout. Only used by tests that need
    /// to exercise the timeout path quickly.
    pub fn with_async_timeout(mut self, timeout: std::time::Duration) -> Self {
        self.async_callback_timeout = timeout;
        self
    }
}

#[async_trait]
impl ToolExecutor for RegistryToolExecutor {
    async fn execute(&self, call: &PbToolCall) -> Result<PbToolResult, CorlinmanError> {
        // Resolve plugin. `call.plugin` is the manifest name; fall back to
        // `call.tool` when the model only emitted a function name (common
        // with OpenAI-standard tool_calls where plugin == tool).
        let name = if !call.plugin.is_empty() {
            call.plugin.as_str()
        } else {
            call.tool.as_str()
        };
        let entry = match self.registry.get(name) {
            Some(e) => e,
            None => {
                return Ok(tool_error_result(
                    &call.call_id,
                    -32601,
                    format!("plugin not found: {name}"),
                ));
            }
        };
        let manifest = entry.manifest.clone();
        let cwd = entry.plugin_dir();

        // Fresh cancellation token per call; later milestones will thread the
        // request-level token here so client disconnects abort in-flight
        // plugin work.
        let cancel = CancellationToken::new();

        let started = std::time::Instant::now();
        let outcome = match manifest.plugin_type {
            PluginType::Service => match self.service_runtime.as_ref() {
                Some(runtime) => {
                    let input = PluginInput {
                        plugin: manifest.name.clone(),
                        tool: call.tool.clone(),
                        args_json: bytes::Bytes::copy_from_slice(&call.args_json),
                        call_id: call.call_id.clone(),
                        session_key: String::new(),
                        trace_id: String::new(),
                        cwd: cwd.clone(),
                        env: Vec::new(),
                        deadline_ms: manifest.communication.timeout_ms,
                    };
                    runtime.execute(input, cancel).await
                }
                None => Err(CorlinmanError::PluginRuntime {
                    plugin: manifest.name.clone(),
                    message: "service plugin runtime not wired on this gateway".into(),
                }),
            },
            PluginType::Sync | PluginType::Async => {
                stdio_execute(
                    &manifest.name,
                    &call.tool,
                    &cwd,
                    Some(&manifest),
                    None, // caller-override deadline; rely on manifest / runtime default
                    &call.args_json,
                    "", // session_key — not yet threaded through chat
                    &call.call_id,
                    "", // trace_id — hooked up with the tracing milestone
                    None,
                    &[],
                    cancel,
                )
                .await
            }
        };
        let elapsed_ms = started.elapsed().as_millis() as u64;

        match outcome {
            Ok(PluginOutput::Success {
                content,
                duration_ms,
            }) => Ok(PbToolResult {
                call_id: call.call_id.clone(),
                result_json: content.to_vec(),
                is_error: false,
                duration_ms,
            }),
            Ok(PluginOutput::Error {
                code,
                message,
                duration_ms,
            }) => Ok(PbToolResult {
                call_id: call.call_id.clone(),
                result_json: serde_json::to_vec(&json!({
                    "code": code,
                    "message": message,
                }))
                .unwrap_or_default(),
                is_error: true,
                duration_ms,
            }),
            Ok(PluginOutput::AcceptedForLater {
                task_id,
                duration_ms,
            }) => {
                // Async plugin: park on the async task registry, wait up to
                // `async_callback_timeout` for `/plugin-callback/:task_id` to
                // deliver the real result. Timeout / disconnect folds into a
                // structured `is_error=true` payload so the reasoning loop
                // never hangs indefinitely.
                let async_tasks = self.registry.async_tasks();
                let rx = async_tasks.register(task_id.clone());
                let wait_started = std::time::Instant::now();
                let wait = tokio::time::timeout(self.async_callback_timeout, rx).await;
                let total_ms =
                    duration_ms.saturating_add(wait_started.elapsed().as_millis() as u64);
                match wait {
                    Ok(Ok(payload)) => {
                        let bytes = serde_json::to_vec(&payload).unwrap_or_default();
                        Ok(PbToolResult {
                            call_id: call.call_id.clone(),
                            result_json: bytes,
                            is_error: false,
                            duration_ms: total_ms,
                        })
                    }
                    Ok(Err(_recv_err)) => {
                        // Sender dropped without calling complete — treat as
                        // an upstream plugin failure so the model retries.
                        Ok(PbToolResult {
                            call_id: call.call_id.clone(),
                            result_json: serde_json::to_vec(&json!({
                                "code": "async_cancelled",
                                "task_id": task_id,
                                "message": "async plugin task cancelled before callback",
                            }))
                            .unwrap_or_default(),
                            is_error: true,
                            duration_ms: total_ms,
                        })
                    }
                    Err(_elapsed) => {
                        // Drop the pending registration so a late callback
                        // gets `NotFound` instead of trying to send into a
                        // closed channel.
                        async_tasks.cancel(&task_id);
                        Ok(PbToolResult {
                            call_id: call.call_id.clone(),
                            result_json: serde_json::to_vec(&json!({
                                "code": "timeout",
                                "task_id": task_id,
                                "message": format!(
                                    "async plugin callback timed out after {}s",
                                    self.async_callback_timeout.as_secs()
                                ),
                            }))
                            .unwrap_or_default(),
                            is_error: true,
                            duration_ms: total_ms,
                        })
                    }
                }
            }
            Err(err) => {
                let mut r =
                    tool_error_result(&call.call_id, runtime_error_code(&err), err.to_string());
                r.duration_ms = elapsed_ms;
                Ok(r)
            }
        }
    }
}

fn tool_error_result(call_id: &str, code: i64, message: String) -> PbToolResult {
    PbToolResult {
        call_id: call_id.to_string(),
        result_json: serde_json::to_vec(&json!({
            "code": code,
            "message": message,
        }))
        .unwrap_or_default(),
        is_error: true,
        duration_ms: 0,
    }
}

/// Per-request wrapper that consults the [`ApprovalGate`] before delegating
/// to the inner executor. Sprint 2 T3: every tool call that reaches the
/// plugin runtime first runs through the gate; `Denied` / `Timeout` short
/// circuit to an `is_error=true` [`PbToolResult`] so the reasoning loop
/// observes a structured failure rather than a hang.
///
/// The session key is captured at request entry so each in-flight chat
/// has its own executor clone — the inner `Arc<dyn ToolExecutor>` and
/// `Arc<ApprovalGate>` are shared cheaply.
pub struct ApprovalToolExecutor {
    inner: Arc<dyn ToolExecutor>,
    gate: Arc<crate::middleware::approval::ApprovalGate>,
    session_key: String,
}

impl ApprovalToolExecutor {
    pub fn new(
        inner: Arc<dyn ToolExecutor>,
        gate: Arc<crate::middleware::approval::ApprovalGate>,
        session_key: String,
    ) -> Self {
        Self {
            inner,
            gate,
            session_key,
        }
    }
}

#[async_trait]
impl ToolExecutor for ApprovalToolExecutor {
    async fn execute(&self, call: &PbToolCall) -> Result<PbToolResult, CorlinmanError> {
        use crate::middleware::approval::ApprovalDecision;

        // Resolve the name the approval rule list was authored against
        // (mirrors RegistryToolExecutor's resolution: `plugin` field wins,
        // fall back to `tool` when the agent emitted a bare function name).
        let plugin = if !call.plugin.is_empty() {
            call.plugin.as_str()
        } else {
            call.tool.as_str()
        };

        // A dedicated per-call cancel token is enough for M3 — the outer
        // request-level token lands in a later milestone (see chat.rs
        // comment around RegistryToolExecutor's own `cancel`).
        let cancel = CancellationToken::new();

        match self
            .gate
            .check(
                &self.session_key,
                plugin,
                &call.tool,
                &call.args_json,
                cancel,
            )
            .await
        {
            Ok(ApprovalDecision::Approved) => self.inner.execute(call).await,
            Ok(ApprovalDecision::Denied(reason)) => Ok(PbToolResult {
                call_id: call.call_id.clone(),
                result_json: serde_json::to_vec(&json!({
                    "code": "approval_denied",
                    "plugin": plugin,
                    "tool": call.tool,
                    "reason": reason,
                }))
                .unwrap_or_default(),
                is_error: true,
                duration_ms: 0,
            }),
            Ok(ApprovalDecision::Timeout) => Ok(PbToolResult {
                call_id: call.call_id.clone(),
                result_json: serde_json::to_vec(&json!({
                    "code": "approval_timeout",
                    "plugin": plugin,
                    "tool": call.tool,
                    "message": "approval request expired before an operator responded",
                }))
                .unwrap_or_default(),
                is_error: true,
                duration_ms: 0,
            }),
            Err(CorlinmanError::Cancelled(_)) => Ok(PbToolResult {
                call_id: call.call_id.clone(),
                result_json: serde_json::to_vec(&json!({
                    "code": "approval_cancelled",
                    "plugin": plugin,
                    "tool": call.tool,
                    "message": "approval wait cancelled",
                }))
                .unwrap_or_default(),
                is_error: true,
                duration_ms: 0,
            }),
            Err(err) => Err(err),
        }
    }
}

fn runtime_error_code(err: &CorlinmanError) -> i64 {
    match err {
        CorlinmanError::Timeout { .. } => -32001,
        CorlinmanError::Cancelled(_) => -32002,
        CorlinmanError::PluginRuntime { .. } => -32010,
        CorlinmanError::Parse { .. } => -32700,
        _ => -32000,
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

async fn handle_chat(
    State(state): State<ChatState>,
    headers: HeaderMap,
    Json(mut req): Json<ChatRequest>,
) -> Response {
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

    // Model alias / unknown-model fallback. Resolution happens BEFORE history
    // load so the request `model` field is final by the time we build the
    // `ChatStart` frame and run downstream provider routing.
    let original_model = req.model.clone();
    match apply_model_aliases(&req.model, &state.model_redirect) {
        ResolvedModel::Aliased { resolved } => {
            tracing::debug!(
                requested = %original_model,
                resolved = %resolved,
                "chat.model.alias_applied"
            );
            req.model = resolved;
        }
        ResolvedModel::Passthrough { .. } => {
            // No rewrite — pass model through verbatim.
        }
        ResolvedModel::FallbackDefault { resolved } => {
            warn!(
                requested = %original_model,
                fallback = %resolved,
                "chat.model.unknown_model_fallback_to_default"
            );
            req.model = resolved;
        }
        ResolvedModel::UnknownNoDefault => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "unknown_model",
                &format!(
                    "model `{original_model}` is not a known alias or provider model, and no `models.default` fallback is configured"
                ),
            );
        }
    }

    // Resolve session_key. Body takes precedence; header `X-Session-Key`
    // is the fallback (mirrors the convention we use for trace headers).
    let session_key = resolve_session_key(&req, &headers);

    // Prepend stored history for this session (if any). Runs BEFORE ChatStart
    // is built so Python sees a single well-ordered messages list. Agent B's
    // model-alias rewrite runs earlier in this same handler (on `req.model`),
    // so the order is: alias apply → load history → prepend → build ChatStart.
    let history_loaded = if let (Some(key), Some(store)) = (&session_key, &state.session_store) {
        match store.load(key).await {
            Ok(msgs) => msgs,
            Err(err) => {
                warn!(session_key = %key, error = %err, "session history load failed; proceeding stateless");
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };

    // Remember the most-recent user message for append (if any). We only
    // persist the trailing user turn the client added this round — earlier
    // user messages in the payload are assumed to already be in history.
    let new_user_message = req
        .messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .cloned();

    if !history_loaded.is_empty() {
        let mut prepended: Vec<ChatMessage> = history_loaded
            .iter()
            .map(session_message_to_chat_message)
            .collect();
        prepended.append(&mut req.messages);
        req.messages = prepended;
    }

    let start = build_chat_start(&req, session_key.as_deref());

    // Build the SSE persist context up front (only for streamed requests with
    // a session store + key). The stream's tail closure drains accumulators
    // and writes to the store just before `[DONE]` so callers that consume the
    // full body observe durable history.
    let persist_ctx = if req.stream {
        match (session_key.as_deref(), state.session_store.as_ref()) {
            (Some(key), Some(store)) => Some(SsePersistCtx {
                store: store.clone(),
                session_key: key.to_string(),
                session_max_messages: state.session_max_messages,
                new_user: new_user_message.clone(),
            }),
            _ => None,
        }
    } else {
        None
    };

    let request_executor = state.request_executor(session_key.as_deref());
    if req.stream {
        match chat_stream(
            state.clone(),
            start,
            req.model,
            persist_ctx,
            request_executor,
        )
        .await
        {
            Ok(sse) => sse.into_response(),
            Err(err) => upstream_error(err),
        }
    } else {
        match chat_nonstream(state.clone(), start, req.model, request_executor).await {
            Ok((resp, assistant_text, tool_calls_json)) => {
                persist_turn(
                    &state,
                    session_key.as_deref(),
                    new_user_message.as_ref(),
                    assistant_text,
                    tool_calls_json,
                )
                .await;
                Json(resp).into_response()
            }
            Err(err) => upstream_error(err),
        }
    }
}

/// Resolve the effective session key from (1) the request body, (2) the
/// `X-Session-Key` header. Empty / whitespace-only strings are treated as
/// absent so a client can't "poison" history by sending `""`.
fn resolve_session_key(req: &ChatRequest, headers: &HeaderMap) -> Option<String> {
    let from_body = req
        .session_key
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    if from_body.is_some() {
        return from_body;
    }
    headers
        .get("x-session-key")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// Convert a persisted [`SessionMessage`] back to the wire-format [`ChatMessage`]
/// we hand to Python. Tool-calls metadata on assistant messages is not currently
/// forwarded through the gRPC frame (proto lacks a slot) — downstream providers
/// reconstruct the reasoning loop from the paired `tool` role messages that
/// follow in history.
fn session_message_to_chat_message(m: &SessionMessage) -> ChatMessage {
    ChatMessage {
        role: match m.role {
            SessionRole::User => "user".into(),
            SessionRole::Assistant => "assistant".into(),
            SessionRole::System => "system".into(),
            SessionRole::Tool => "tool".into(),
        },
        content: m.content.clone(),
        name: None,
        tool_call_id: m.tool_call_id.clone(),
    }
}

/// Append the new user + assistant messages for this turn and schedule an
/// async trim. No-op when session_key or store is absent.
async fn persist_turn(
    state: &ChatState,
    session_key: Option<&str>,
    new_user: Option<&ChatMessage>,
    assistant_text: String,
    tool_calls_json: Option<Value>,
) {
    let (Some(key), Some(store)) = (session_key, state.session_store.as_ref()) else {
        return;
    };
    if let Some(user) = new_user {
        let msg = SessionMessage::user(user.content.clone());
        if let Err(err) = store.append(key, msg).await {
            warn!(session_key = %key, error = %err, "session append(user) failed");
        }
    }
    let assistant = SessionMessage::assistant(assistant_text, tool_calls_json);
    if let Err(err) = store.append(key, assistant).await {
        warn!(session_key = %key, error = %err, "session append(assistant) failed");
    }
    // Trim asynchronously — it's a background maintenance op and must not
    // block the response.
    let store = store.clone();
    let key = key.to_string();
    let keep = state.session_max_messages;
    tokio::spawn(async move {
        if let Err(err) = store.trim(&key, keep).await {
            warn!(session_key = %key, error = %err, "session trim failed");
        }
    });
}

// ---- Non-streaming ------------------------------------------------------------

async fn chat_nonstream(
    state: ChatState,
    start: ChatStart,
    model: String,
    tool_executor: Arc<dyn ToolExecutor>,
) -> Result<(ChatResponse, String, Option<Value>), CorlinmanError> {
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
                let result = tool_executor.execute(&tc).await?;
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

    let tool_calls_json = if tool_calls.is_empty() {
        None
    } else {
        serde_json::to_value(&tool_calls).ok()
    };
    let assistant_text = content.clone();

    let response = ChatResponse {
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
    };
    Ok((response, assistant_text, tool_calls_json))
}

// ---- Streaming (SSE) ----------------------------------------------------------

/// Parameters for persisting a streamed turn after the SSE stream drains.
/// `None` = no session persistence for this request.
#[derive(Clone)]
struct SsePersistCtx {
    store: Arc<dyn SessionStore>,
    session_key: String,
    session_max_messages: usize,
    /// The user message this turn added (persisted first, before the
    /// assistant response, so the ordering matches non-stream).
    new_user: Option<ChatMessage>,
}

async fn chat_stream(
    state: ChatState,
    start: ChatStart,
    model: String,
    persist: Option<SsePersistCtx>,
    tool_executor: Arc<dyn ToolExecutor>,
) -> Result<Sse<impl Stream<Item = Result<SseEvent, Infallible>>>, CorlinmanError> {
    let (tx, rx) = state.backend.start(start).await?;
    let id = format!("chatcmpl-{}", Uuid::new_v4());

    let sse_stream = build_sse_stream(rx, tx, tool_executor, id, model, persist);
    Ok(Sse::new(sse_stream).keep_alive(KeepAlive::default()))
}

fn build_sse_stream(
    rx: BackendRx,
    tx: mpsc::Sender<ClientFrame>,
    executor: Arc<dyn ToolExecutor>,
    id: String,
    model: String,
    persist: Option<SsePersistCtx>,
) -> impl Stream<Item = Result<SseEvent, Infallible>> + Send {
    use futures::StreamExt;

    // Accumulators shared between the unfold body (which appends as frames
    // arrive) and the tail closure (which persists the finished assistant
    // message). A single async `Mutex` is plenty — the SSE pump is driven by
    // one task so there is no contention, and both accessors are `await`-safe.
    let accum_text: Arc<tokio::sync::Mutex<String>> =
        Arc::new(tokio::sync::Mutex::new(String::new()));
    let accum_tool_calls: Arc<tokio::sync::Mutex<Vec<Value>>> =
        Arc::new(tokio::sync::Mutex::new(Vec::new()));

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
        /// Shared accumulator for the assistant text (token deltas
        /// concatenated). Read by the tail closure for session persistence.
        accum_text: Arc<tokio::sync::Mutex<String>>,
        accum_tool_calls: Arc<tokio::sync::Mutex<Vec<Value>>>,
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
        accum_text: accum_text.clone(),
        accum_tool_calls: accum_tool_calls.clone(),
    };

    stream::unfold(state, |mut s| async move {
        if s.done {
            return None;
        }
        loop {
            match s.rx.next().await {
                Some(Ok(frame)) => match frame.kind {
                    Some(server_frame::Kind::Token(t)) => {
                        s.accum_text.lock().await.push_str(&t.text);
                        let chunk = token_delta_chunk(&s.id, &s.model, &t.text);
                        let ev = SseEvent::default().data(chunk.to_string());
                        return Some((Ok(ev), s));
                    }
                    Some(server_frame::Kind::ToolCall(tc)) => {
                        let idx = s.next_tool_index;
                        s.next_tool_index += 1;
                        s.tool_calls_seen = true;
                        if let Ok(val) = serde_json::to_value(pb_tool_call_to_openai(&tc)) {
                            s.accum_tool_calls.lock().await.push(val);
                        }

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
    .chain(stream::once(async move {
        // Persist after the core stream drained — runs before the `[DONE]`
        // sentinel so a client that consumes the whole body can rely on
        // history being durable.
        if let Some(ctx) = persist {
            let text = std::mem::take(&mut *accum_text.lock().await);
            let calls = std::mem::take(&mut *accum_tool_calls.lock().await);
            let tool_calls_json = if calls.is_empty() {
                None
            } else {
                Some(Value::Array(calls))
            };
            persist_stream_turn(ctx, text, tool_calls_json).await;
        }
        Ok::<_, Infallible>(SseEvent::default().data("[DONE]"))
    }))
}

/// Persist the streamed turn's user + assistant messages. Mirrors the
/// non-stream `persist_turn` — factored separately because the stream path
/// doesn't have `ChatState` handy at the point of invocation.
async fn persist_stream_turn(
    ctx: SsePersistCtx,
    assistant_text: String,
    tool_calls_json: Option<Value>,
) {
    let SsePersistCtx {
        store,
        session_key,
        session_max_messages,
        new_user,
    } = ctx;
    if let Some(user) = new_user.as_ref() {
        let msg = SessionMessage::user(user.content.clone());
        if let Err(err) = store.append(&session_key, msg).await {
            warn!(session_key = %session_key, error = %err, "session append(user) failed");
        }
    }
    let assistant = SessionMessage::assistant(assistant_text, tool_calls_json);
    if let Err(err) = store.append(&session_key, assistant).await {
        warn!(session_key = %session_key, error = %err, "session append(assistant) failed");
    }
    let key = session_key;
    tokio::spawn(async move {
        if let Err(err) = store.trim(&key, session_max_messages).await {
            warn!(session_key = %key, error = %err, "session trim failed");
        }
    });
}

// ---- Helpers ------------------------------------------------------------------

fn build_chat_start(req: &ChatRequest, session_key: Option<&str>) -> ChatStart {
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
        session_key: session_key.unwrap_or("").to_string(),
        binding: None,
        placeholders: Default::default(),
        temperature: req.temperature.unwrap_or(0.0),
        max_tokens: req.max_tokens.unwrap_or(0),
        stream: req.stream,
        trace: None,
        provider_config_json: Default::default(),
        // HTTP REST body carries no attachments today; multimodal inputs flow
        // through the in-process ChatService (channels path). See
        // `services::chat_service::build_chat_start` for the real conversion.
        attachments: Vec::new(),
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
        /// Captures the last `ChatStart` passed to `start()` so tests can
        /// assert on model-redirect rewrites without introducing a second
        /// mock impl.
        pub captured_start: Arc<tokio::sync::Mutex<Option<ChatStart>>>,
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
            start: ChatStart,
        ) -> Result<(mpsc::Sender<ClientFrame>, BackendRx), CorlinmanError> {
            if self.fail_on_start {
                return Err(CorlinmanError::Upstream {
                    reason: corlinman_core::FailoverReason::Overloaded,
                    message: "mock backend refused".into(),
                });
            }
            *self.captured_start.lock().await = Some(start);
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

    // ---- Model redirect (T3) ----------------------------------------------

    fn app_with_redirect(backend: Arc<dyn ChatBackend>, redirect: ModelRedirect) -> Router {
        router_with_state(ChatState::new(backend).with_model_redirect(redirect))
    }

    #[tokio::test]
    async fn model_redirect_alias_rewrites_requested_model() {
        // Alias "sonnet" → "claude-sonnet-4-5" must flow through to the
        // ChatStart frame the backend observes.
        let backend = Arc::new(MockBackend::with_frames(vec![token("ok"), done("stop")]));
        let captured_start = backend.captured_start.clone();
        let mut aliases = std::collections::HashMap::new();
        aliases.insert("sonnet".into(), "claude-sonnet-4-5".into());
        let redirect = ModelRedirect::new(aliases, String::new(), Default::default());
        let app = app_with_redirect(backend, redirect);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "sonnet",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        // The backend must have observed the rewritten model.
        let captured = captured_start.lock().await.clone().expect("start captured");
        assert_eq!(captured.model, "claude-sonnet-4-5");

        // The OpenAI response `model` echo also reflects the rewrite — the
        // handler forwards `req.model` (post-redirect) to `chat_nonstream`.
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["model"], "claude-sonnet-4-5");
    }

    #[tokio::test]
    async fn model_redirect_unknown_model_falls_back_to_default() {
        // Unknown model + non-empty known_models + default set → warn + swap
        // in `default`.
        let backend = Arc::new(MockBackend::with_frames(vec![token("ok"), done("stop")]));
        let captured_start = backend.captured_start.clone();
        let mut known = std::collections::HashSet::new();
        known.insert("claude-sonnet-4-5".to_string());
        let redirect = ModelRedirect::new(Default::default(), "claude-sonnet-4-5".into(), known);
        let app = app_with_redirect(backend, redirect);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "gpt-unknown-99",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        let captured = captured_start.lock().await.clone().expect("start captured");
        assert_eq!(captured.model, "claude-sonnet-4-5");
    }

    #[tokio::test]
    async fn model_redirect_unknown_model_without_default_returns_400() {
        // Unknown model + non-empty known_models + empty default → 400.
        let backend = Arc::new(MockBackend::with_frames(vec![token("ok"), done("stop")]));
        let mut known = std::collections::HashSet::new();
        known.insert("claude-sonnet-4-5".to_string());
        let redirect = ModelRedirect::new(Default::default(), String::new(), known);
        let app = app_with_redirect(backend, redirect);

        let req = Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&json!({
                    "model": "gpt-unknown-99",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": false
                }))
                .unwrap(),
            ))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(v["error"]["code"], "unknown_model");
        let msg = v["error"]["message"].as_str().unwrap();
        assert!(
            msg.contains("gpt-unknown-99"),
            "error must name the rejected model: {msg}"
        );
    }

    #[test]
    fn apply_model_aliases_pure_function_covers_all_outcomes() {
        // Pure-function coverage so the handler integration tests can stay
        // focused on wiring. Covers the four ResolvedModel variants.
        let mut aliases = std::collections::HashMap::new();
        aliases.insert("alias-a".to_string(), "target-a".to_string());
        let mut known = std::collections::HashSet::new();
        known.insert("target-a".to_string());
        known.insert("known-direct".to_string());

        // Alias hit.
        let r = apply_model_aliases(
            "alias-a",
            &ModelRedirect::new(aliases.clone(), "fallback".into(), known.clone()),
        );
        assert!(matches!(r, ResolvedModel::Aliased { ref resolved } if resolved == "target-a"));

        // Known model → passthrough.
        let r = apply_model_aliases(
            "known-direct",
            &ModelRedirect::new(aliases.clone(), "fallback".into(), known.clone()),
        );
        assert!(
            matches!(r, ResolvedModel::Passthrough { ref resolved } if resolved == "known-direct")
        );

        // Unknown + default → fallback.
        let r = apply_model_aliases(
            "bogus",
            &ModelRedirect::new(aliases.clone(), "fallback".into(), known.clone()),
        );
        assert!(
            matches!(r, ResolvedModel::FallbackDefault { ref resolved } if resolved == "fallback")
        );

        // Unknown + no default → 400 sentinel.
        let r = apply_model_aliases("bogus", &ModelRedirect::new(aliases, String::new(), known));
        assert_eq!(r, ResolvedModel::UnknownNoDefault);

        // Empty known_models → every model passes through.
        let r = apply_model_aliases(
            "arbitrary",
            &ModelRedirect::new(Default::default(), "ignored".into(), Default::default()),
        );
        assert!(
            matches!(r, ResolvedModel::Passthrough { ref resolved } if resolved == "arbitrary")
        );
    }
}
