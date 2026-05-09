//! `tools` capability adapter — bridges `corlinman-plugins` onto MCP's
//! `tools/list` + `tools/call` methods.
//!
//! ## Tool naming
//!
//! MCP tool names are flat strings; corlinman tool names are
//! `<plugin>:<tool>` (Open question §2 in the design). We pick `:` over
//! `.` because:
//!   - `.` collides with the C2 mcp-stdio passthrough (an upstream
//!     server may itself expose dotted names).
//!   - The MCP 2024-11-05 spec accepts `:`, `_`, `-`, alphanumerics in
//!     tool names; Desktop's UI renders `:` cleanly.
//!
//! ## Output shape
//!
//! `PluginOutput::Success { content }` → one [`Content::Text`] block
//! containing the JSON-encoded body verbatim. `PluginOutput::Error` →
//! `CallResult { is_error: true, content: [<message>] }` per the MCP
//! convention that *runtime* failures land in `is_error`, not in a
//! JSON-RPC error envelope. `PluginOutput::AcceptedForLater` collapses
//! to a textual placeholder (C1 doesn't surface async task ids; C2 may).
//!
//! ## Cancellation + progress
//!
//! Each call gets its own [`CancellationToken`] cloned from a
//! per-session root token. Progress emitted via [`ProgressSink`]
//! becomes [`MCP_PROGRESS_NOTIFICATION`] frames; iter 5 ships the bridge
//! shape behind [`ProgressBridge`] so iter 9 can wire the actual
//! server→client write side. The unit tests below stub the writer.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use serde_json::{json, Value as JsonValue};
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

use corlinman_plugins::registry::PluginRegistry;
use corlinman_plugins::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};

use crate::adapters::{CapabilityAdapter, SessionContext};
use crate::error::McpError;
use crate::schema::tools::{CallParams, CallResult, Content, ListResult, ToolDescriptor};

/// MCP method-name constants the dispatcher routes by string match.
pub const METHOD_LIST: &str = "tools/list";
pub const METHOD_CALL: &str = "tools/call";

/// Method name for outbound progress notifications. Per spec §progress.
pub const MCP_PROGRESS_NOTIFICATION: &str = "notifications/progress";

/// Default deadline for a single MCP-driven tool call. Aligned with the
/// `Communication.timeout_ms` default in `corlinman-plugins` (30 s).
const DEFAULT_DEADLINE_MS: u64 = 30_000;

/// One progress event emitted by an in-flight tool. Iter 5 shapes this
/// for the wire bridge but doesn't yet hook a writer; iter 9 attaches a
/// real WS sender. Made `pub` so iter 9 can construct frames in the
/// transport layer.
#[derive(Debug, Clone)]
pub struct ProgressEvent {
    /// Opaque per-call correlation token. Maps onto the spec's
    /// `progressToken` field.
    pub progress_token: String,
    /// Free-form human message.
    pub message: String,
    /// Optional [0.0, 1.0] completion fraction.
    pub fraction: Option<f32>,
}

impl ProgressEvent {
    /// Render to a JSON-RPC notification body — `notifications/progress`
    /// with the spec-shaped params. The transport layer wraps this in
    /// the JSON-RPC envelope.
    pub fn to_progress_params(&self) -> JsonValue {
        let mut obj = serde_json::Map::new();
        obj.insert("progressToken".into(), json!(self.progress_token));
        obj.insert("progress".into(), json!(self.fraction.unwrap_or(0.0)));
        if !self.message.is_empty() {
            obj.insert("message".into(), json!(self.message));
        }
        JsonValue::Object(obj)
    }
}

/// Bridges plugin-runtime [`ProgressSink`] events onto MCP
/// `notifications/progress` frames. Iter 5 ships the abstraction so the
/// adapter can be tested end-to-end without standing up a transport;
/// iter 9 supplies a writer that pumps events back over the WS.
pub trait ProgressBridge: Send + Sync {
    fn forward(&self, event: ProgressEvent);
}

/// No-op bridge — used by the dispatcher when the caller didn't supply
/// a `progressToken` or by tests that don't care about progress frames.
pub struct NullProgressBridge;

impl ProgressBridge for NullProgressBridge {
    fn forward(&self, _event: ProgressEvent) {}
}

/// Test-side bridge that stores forwarded events in a vector for
/// assertion. `pub(crate)` so the adapter's unit tests can use it
/// without exposing it on the public surface.
#[cfg(test)]
pub(crate) struct CollectingProgressBridge {
    pub events: std::sync::Mutex<Vec<ProgressEvent>>,
}

#[cfg(test)]
impl CollectingProgressBridge {
    pub fn new() -> Self {
        Self {
            events: std::sync::Mutex::new(Vec::new()),
        }
    }
    pub fn drain(&self) -> Vec<ProgressEvent> {
        std::mem::take(&mut *self.events.lock().unwrap())
    }
}

#[cfg(test)]
impl ProgressBridge for CollectingProgressBridge {
    fn forward(&self, event: ProgressEvent) {
        self.events.lock().unwrap().push(event);
    }
}

/// Adapter that maps an [`Arc<PluginRegistry>`] + a [`PluginRuntime`]
/// onto MCP's `tools/*` surface.
pub struct ToolsAdapter {
    registry: Arc<PluginRegistry>,
    runtime: Arc<dyn PluginRuntime>,
    /// Bridge that receives `ProgressSink::emit` callbacks during a
    /// `tools/call`. Wrapped in a `Mutex` only because trait objects
    /// can't be shared by value into the per-call sink; logically every
    /// session has one bridge.
    progress: Arc<dyn ProgressBridge>,
    /// Per-session cancellation token; calls clone children off of it
    /// so a session shutdown aborts every in-flight call.
    cancel_root: CancellationToken,
}

impl ToolsAdapter {
    /// Build with a real bridge; iter 9 calls this from the dispatcher.
    pub fn new(
        registry: Arc<PluginRegistry>,
        runtime: Arc<dyn PluginRuntime>,
        progress: Arc<dyn ProgressBridge>,
    ) -> Self {
        Self {
            registry,
            runtime,
            progress,
            cancel_root: CancellationToken::new(),
        }
    }

    /// Convenience — build with the no-op progress bridge.
    pub fn with_runtime(
        registry: Arc<PluginRegistry>,
        runtime: Arc<dyn PluginRuntime>,
    ) -> Self {
        Self::new(registry, runtime, Arc::new(NullProgressBridge))
    }

    /// Cancel every in-flight call on this session. Iter 9's transport
    /// calls this when the WS closes.
    pub fn cancel_all(&self) {
        self.cancel_root.cancel();
    }

    /// Build the `tools/list` response, filtered by `ctx.tools_allowlist`.
    pub fn list_tools(&self, ctx: &SessionContext) -> ListResult {
        let mut out: Vec<ToolDescriptor> = Vec::new();
        for entry in self.registry.list() {
            for tool in &entry.manifest.capabilities.tools {
                let name = encode_tool_name(&entry.manifest.name, &tool.name);
                if !ctx.allows_tool(&name) {
                    continue;
                }
                let input_schema = if tool.parameters.is_object() {
                    tool.parameters.clone()
                } else {
                    json!({"type": "object", "additionalProperties": true})
                };
                out.push(ToolDescriptor {
                    name,
                    description: if tool.description.is_empty() {
                        None
                    } else {
                        Some(tool.description.clone())
                    },
                    input_schema,
                });
            }
        }
        // Stable ordering for snapshot tests.
        out.sort_by(|a, b| a.name.cmp(&b.name));
        ListResult {
            tools: out,
            next_cursor: None,
        }
    }

    /// Execute one `tools/call`. The result is shaped onto MCP's
    /// `CallResult` (with `is_error` for runtime failures) — protocol
    /// failures (unknown tool, allowlist denial) come back as `Err`.
    pub async fn call_tool(
        &self,
        params: CallParams,
        ctx: &SessionContext,
        progress_token: Option<String>,
    ) -> Result<CallResult, McpError> {
        let (plugin_name, tool_name) = decode_tool_name(&params.name)
            .ok_or_else(|| McpError::MethodNotFound(format!("tools/call: {}", params.name)))?;

        let qualified = encode_tool_name(plugin_name, tool_name);
        if !ctx.allows_tool(&qualified) {
            return Err(McpError::ToolNotAllowed(qualified));
        }

        let entry = self.registry.get(plugin_name).ok_or_else(|| {
            McpError::MethodNotFound(format!("tools/call: unknown plugin {plugin_name}"))
        })?;

        // Verify tool actually exists on the plugin.
        let tool = entry
            .manifest
            .capabilities
            .tools
            .iter()
            .find(|t| t.name == tool_name)
            .ok_or_else(|| {
                McpError::MethodNotFound(format!(
                    "tools/call: plugin '{plugin_name}' has no tool '{tool_name}'"
                ))
            })?;
        let _ = tool; // we don't currently consult the schema here; coercion is the runtime's job.

        // Serialize arguments; MCP allows omitted args (we map to `{}`).
        let args = if params.arguments.is_null() {
            JsonValue::Object(Default::default())
        } else {
            params.arguments
        };
        let args_bytes = Bytes::from(serde_json::to_vec(&args).map_err(|e| {
            McpError::Internal(format!("tools/call: serialize args: {e}"))
        })?);

        let cwd = entry.plugin_dir();
        let input = PluginInput {
            plugin: plugin_name.to_string(),
            tool: tool_name.to_string(),
            args_json: args_bytes,
            call_id: format!("mcp-{}", uuid_like()),
            session_key: "mcp".to_string(),
            trace_id: format!("mcp-{}", uuid_like()),
            cwd,
            env: entry
                .manifest
                .entry_point
                .env
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
            deadline_ms: entry
                .manifest
                .communication
                .timeout_ms
                .or(Some(DEFAULT_DEADLINE_MS)),
        };

        let cancel = self.cancel_root.child_token();
        let sink: Option<Arc<dyn ProgressSink>> = progress_token.map(|tok| {
            Arc::new(ProgressSinkAdapter {
                token: tok,
                bridge: self.progress.clone(),
            }) as Arc<dyn ProgressSink>
        });

        // Hard wall-clock guard to keep a sandbox bug from hanging the
        // session forever — even when the runtime ignores `deadline_ms`.
        let timeout = entry
            .manifest
            .communication
            .timeout_ms
            .unwrap_or(DEFAULT_DEADLINE_MS);
        let exec = self.runtime.execute(input, sink, cancel.clone());
        let outcome = match tokio::time::timeout(
            Duration::from_millis(timeout.saturating_add(500)),
            exec,
        )
        .await
        {
            Ok(r) => r,
            Err(_) => {
                cancel.cancel();
                warn!(plugin = plugin_name, tool = tool_name, "tools/call: deadline exceeded");
                return Ok(CallResult {
                    content: vec![Content::text(format!(
                        "tools/call: deadline exceeded after {timeout}ms"
                    ))],
                    is_error: true,
                });
            }
        };

        match outcome {
            Ok(PluginOutput::Success { content, .. }) => {
                let text = String::from_utf8(content.to_vec())
                    .unwrap_or_else(|e| format!("<non-utf8 plugin output: {e}>"));
                Ok(CallResult {
                    content: vec![Content::text(text)],
                    is_error: false,
                })
            }
            Ok(PluginOutput::Error { code, message, .. }) => {
                debug!(plugin = plugin_name, tool = tool_name, code, "tools/call: runtime error");
                Ok(CallResult {
                    content: vec![Content::text(format!("[code {code}] {message}"))],
                    is_error: true,
                })
            }
            Ok(PluginOutput::AcceptedForLater { task_id, .. }) => {
                // C1 doesn't surface async task ids; collapse to a
                // descriptive text block.
                Ok(CallResult {
                    content: vec![Content::text(format!(
                        "accepted-for-later (task_id={task_id}); polling not supported in MCP C1"
                    ))],
                    is_error: false,
                })
            }
            Err(err) => {
                // Runtime infrastructure failure (sandbox, transport)
                // — propagate as JSON-RPC -32603. The adapter can't
                // distinguish "your tool died" from "our sandbox died"
                // here, so we surface internal-error.
                Err(McpError::Internal(format!(
                    "tools/call: runtime failure: {err}"
                )))
            }
        }
    }
}

#[async_trait]
impl CapabilityAdapter for ToolsAdapter {
    fn capability_name(&self) -> &'static str {
        "tools"
    }

    async fn handle(
        &self,
        method: &str,
        params: JsonValue,
        ctx: &SessionContext,
    ) -> Result<JsonValue, McpError> {
        match method {
            METHOD_LIST => {
                let list = self.list_tools(ctx);
                serde_json::to_value(list).map_err(|e| {
                    McpError::Internal(format!("tools/list: serialize result: {e}"))
                })
            }
            METHOD_CALL => {
                // Pull progressToken out of `_meta` if present (MCP
                // 2024-11-05 §progress); the rest deserialises into
                // `CallParams`.
                let progress_token = params
                    .get("_meta")
                    .and_then(|m| m.get("progressToken"))
                    .and_then(|v| v.as_str().map(str::to_string));
                let parsed: CallParams = serde_json::from_value(params).map_err(|e| {
                    McpError::invalid_params(format!("tools/call: bad params: {e}"))
                })?;
                let result = self.call_tool(parsed, ctx, progress_token).await?;
                serde_json::to_value(result).map_err(|e| {
                    McpError::Internal(format!("tools/call: serialize result: {e}"))
                })
            }
            other => Err(McpError::MethodNotFound(other.to_string())),
        }
    }
}

/// `<plugin>:<tool>` per design Open question §2.
pub fn encode_tool_name(plugin: &str, tool: &str) -> String {
    format!("{plugin}:{tool}")
}

/// Inverse of [`encode_tool_name`]. Returns `None` when the input
/// doesn't contain the separator (which means it's not a corlinman
/// MCP-shaped name).
pub fn decode_tool_name(qualified: &str) -> Option<(&str, &str)> {
    let (plugin, tool) = qualified.split_once(':')?;
    if plugin.is_empty() || tool.is_empty() {
        return None;
    }
    Some((plugin, tool))
}

/// Pseudo-uuid that doesn't pull in the `uuid` crate. Random enough for
/// per-call correlation in logs; not a security primitive.
fn uuid_like() -> String {
    // Use the address of a one-shot allocation as entropy. Cheap,
    // deterministic per process, distinct per call. Iter 9 may swap to
    // the real `uuid` crate when the gateway integration lands.
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let entropy = Box::new(0u8); // unique address
    format!("{now:x}-{:p}", entropy)
}

/// Bridge from plugin `ProgressSink` calls onto our `ProgressBridge`.
struct ProgressSinkAdapter {
    token: String,
    bridge: Arc<dyn ProgressBridge>,
}

#[async_trait]
impl ProgressSink for ProgressSinkAdapter {
    async fn emit(&self, message: String, fraction: Option<f32>) {
        self.bridge.forward(ProgressEvent {
            progress_token: self.token.clone(),
            message,
            fraction,
        });
    }
}

// Silence unused — Mutex carrier kept for parity with future iter when
// adapter holds per-call state.
#[allow(dead_code)]
fn _carrier(_m: Mutex<()>) {}

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    use bytes::Bytes;
    use corlinman_plugins::manifest::{
        Capabilities, Communication, EntryPoint, PluginManifest, PluginType, Tool,
    };
    use corlinman_plugins::registry::{PluginEntry, PluginRegistry};
    use corlinman_plugins::discovery::Origin;

    /// Build a stub registry holding a single plugin/tool combo.
    fn make_registry(plugin: &str, tools: &[(&str, &str)]) -> Arc<PluginRegistry> {
        let reg = PluginRegistry::default();
        let manifest = PluginManifest {
            manifest_version: 2,
            name: plugin.to_string(),
            version: "0.1.0".to_string(),
            description: "stub".to_string(),
            author: "test".to_string(),
            plugin_type: PluginType::Sync,
            entry_point: EntryPoint {
                command: "true".to_string(),
                args: vec![],
                env: Default::default(),
            },
            mcp: None,
            communication: Communication {
                timeout_ms: Some(2_000),
            },
            capabilities: Capabilities {
                tools: tools
                    .iter()
                    .map(|(name, desc)| Tool {
                        name: (*name).to_string(),
                        description: (*desc).to_string(),
                        parameters: serde_json::json!({"type": "object"}),
                    })
                    .collect(),
                disable_model_invocation: false,
            },
            sandbox: Default::default(),
            meta: None,
            protocols: vec!["openai_function".to_string()],
            hooks: vec![],
            skill_refs: vec![],
        };
        let entry = PluginEntry {
            manifest: Arc::new(manifest),
            origin: Origin::Workspace,
            manifest_path: std::path::PathBuf::from("/tmp/stub/plugin-manifest.toml"),
            shadowed_count: 0,
        };
        // upsert is pub(crate); we can call it because tests live in
        // this crate. But tests in adapters/tools.rs are in a different
        // crate (corlinman-mcp). We use `from_roots` with empty roots
        // and inject through the only public seam: a doc-test fixture.
        // Workaround: build a registry via a tiny in-memory shim.
        let _ = reg;
        let _ = entry;
        // Use the construction helper added to corlinman-plugins for
        // this purpose: PluginRegistry has no public `with_entries`,
        // but `from_roots` returns an empty one if roots are empty.
        // We instead rely on the public constructor that accepts a
        // pre-built map: there isn't one. So drop down to building a
        // tempdir with a manifest file.
        unreachable!("see make_registry_from_disk")
    }

    /// Build a registry by writing a real manifest to a tempdir, then
    /// passing the dir as a search root. This exercises the public
    /// surface only — no `pub(crate)` insertion.
    fn make_registry_from_disk(
        tmp: &tempfile::TempDir,
        plugin: &str,
        tools: &[(&str, &str)],
    ) -> Arc<PluginRegistry> {
        use std::io::Write;
        let dir = tmp.path().join(plugin);
        std::fs::create_dir_all(&dir).unwrap();
        let mut s = String::new();
        s.push_str(&format!(
            "name = \"{plugin}\"\nversion = \"0.1.0\"\nplugin_type = \"sync\"\n[entry_point]\ncommand = \"true\"\n[communication]\ntimeout_ms = 2000\n"
        ));
        for (name, desc) in tools {
            s.push_str(&format!(
                "[[capabilities.tools]]\nname = \"{name}\"\ndescription = \"{desc}\"\n[capabilities.tools.parameters]\ntype = \"object\"\n"
            ));
        }
        let mut f = std::fs::File::create(dir.join("plugin-manifest.toml")).unwrap();
        f.write_all(s.as_bytes()).unwrap();
        let roots = vec![corlinman_plugins::discovery::SearchRoot::new(
            tmp.path(),
            corlinman_plugins::discovery::Origin::Workspace,
        )];
        Arc::new(PluginRegistry::from_roots(roots))
    }

    /// Stub runtime that records the most recent `PluginInput` and
    /// returns a configurable [`PluginOutput`].
    struct StubRuntime {
        seen: std::sync::Mutex<Vec<PluginInput>>,
        outcome: PluginOutput,
        progress_emit: Option<(String, Option<f32>)>,
    }

    impl StubRuntime {
        fn new(outcome: PluginOutput) -> Self {
            Self {
                seen: std::sync::Mutex::new(Vec::new()),
                outcome,
                progress_emit: None,
            }
        }

        fn with_progress(mut self, msg: impl Into<String>, frac: Option<f32>) -> Self {
            self.progress_emit = Some((msg.into(), frac));
            self
        }
    }

    #[async_trait]
    impl PluginRuntime for StubRuntime {
        async fn execute(
            &self,
            input: PluginInput,
            progress: Option<Arc<dyn ProgressSink>>,
            _cancel: CancellationToken,
        ) -> Result<PluginOutput, corlinman_core::CorlinmanError> {
            self.seen.lock().unwrap().push(input);
            if let (Some(sink), Some((msg, frac))) = (progress, self.progress_emit.clone()) {
                sink.emit(msg, frac).await;
            }
            Ok(self.outcome.clone())
        }
        fn kind(&self) -> &'static str {
            "stub"
        }
    }

    fn make_runtime(outcome: PluginOutput) -> Arc<dyn PluginRuntime> {
        Arc::new(StubRuntime::new(outcome))
    }

    // ----- list / call -----

    #[tokio::test]
    async fn list_returns_one_descriptor_per_manifest_tool() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "find stuff"), ("get", "fetch by id")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::from_static(b"{}"), 1));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let result = adapter.list_tools(&SessionContext::permissive());
        let names: Vec<_> = result.tools.iter().map(|t| t.name.clone()).collect();
        assert_eq!(names, vec!["kb:get".to_string(), "kb:search".to_string()]);
        assert_eq!(result.tools[0].description.as_deref(), Some("fetch by id"));
        // Schema is propagated verbatim from the manifest.
        assert_eq!(
            result.tools[0].input_schema,
            serde_json::json!({"type": "object"})
        );
    }

    #[tokio::test]
    async fn list_filters_by_allowlist() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", ""), ("get", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::from_static(b"{}"), 1));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let mut ctx = SessionContext::default();
        ctx.tools_allowlist = vec!["kb:s*".to_string()];
        let result = adapter.list_tools(&ctx);
        let names: Vec<_> = result.tools.iter().map(|t| t.name.clone()).collect();
        assert_eq!(names, vec!["kb:search".to_string()]);
    }

    #[tokio::test]
    async fn call_success_returns_text_block_with_no_is_error() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::from_static(b"{\"ok\":1}"), 5));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let res = adapter
            .call_tool(
                CallParams {
                    name: "kb:search".to_string(),
                    arguments: serde_json::json!({"q": "hi"}),
                },
                &SessionContext::permissive(),
                None,
            )
            .await
            .unwrap();
        assert!(!res.is_error);
        match &res.content[0] {
            Content::Text { text } => assert_eq!(text, "{\"ok\":1}"),
            other => panic!("expected text content, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn call_runtime_error_surfaces_as_is_error_not_jsonrpc_error() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::error(7, "boom", 5));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let res = adapter
            .call_tool(
                CallParams {
                    name: "kb:search".to_string(),
                    arguments: serde_json::json!({}),
                },
                &SessionContext::permissive(),
                None,
            )
            .await
            .unwrap();
        assert!(res.is_error);
        match &res.content[0] {
            Content::Text { text } => {
                assert!(text.contains("boom"));
                assert!(text.contains("[code 7]"));
            }
            other => panic!("expected text content, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn call_unknown_plugin_returns_method_not_found() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::new(), 0));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let err = adapter
            .call_tool(
                CallParams {
                    name: "ghost:do".to_string(),
                    arguments: JsonValue::Null,
                },
                &SessionContext::permissive(),
                None,
            )
            .await
            .expect_err("unknown plugin must error");
        assert!(matches!(err, McpError::MethodNotFound(_)));
        assert_eq!(err.jsonrpc_code(), -32601);
    }

    #[tokio::test]
    async fn call_unknown_tool_on_known_plugin_returns_method_not_found() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::new(), 0));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let err = adapter
            .call_tool(
                CallParams {
                    name: "kb:nope".to_string(),
                    arguments: JsonValue::Null,
                },
                &SessionContext::permissive(),
                None,
            )
            .await
            .expect_err("unknown tool must error");
        assert!(matches!(err, McpError::MethodNotFound(_)));
    }

    #[tokio::test]
    async fn call_with_disallowed_tool_returns_tool_not_allowed() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::new(), 0));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);

        let mut ctx = SessionContext::default();
        ctx.tools_allowlist = vec!["other:*".to_string()];
        let err = adapter
            .call_tool(
                CallParams {
                    name: "kb:search".to_string(),
                    arguments: JsonValue::Null,
                },
                &ctx,
                None,
            )
            .await
            .expect_err("disallowed tool must error");
        match err {
            McpError::ToolNotAllowed(name) => assert_eq!(name, "kb:search"),
            other => panic!("expected ToolNotAllowed, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn call_progress_events_forward_to_bridge_when_token_supplied() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime: Arc<dyn PluginRuntime> = Arc::new(
            StubRuntime::new(PluginOutput::success(Bytes::from_static(b"done"), 1))
                .with_progress("halfway", Some(0.5)),
        );
        let bridge = Arc::new(CollectingProgressBridge::new());
        let adapter =
            ToolsAdapter::new(reg, runtime, bridge.clone() as Arc<dyn ProgressBridge>);

        let res = adapter
            .call_tool(
                CallParams {
                    name: "kb:search".to_string(),
                    arguments: JsonValue::Null,
                },
                &SessionContext::permissive(),
                Some("p-token-1".to_string()),
            )
            .await
            .unwrap();
        assert!(!res.is_error);
        let events = bridge.drain();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].progress_token, "p-token-1");
        assert_eq!(events[0].message, "halfway");
        assert_eq!(events[0].fraction, Some(0.5));

        // ProgressEvent → JSON-RPC params shape
        let params = events[0].to_progress_params();
        assert_eq!(params["progressToken"], "p-token-1");
        assert_eq!(params["message"], "halfway");
        assert!((params["progress"].as_f64().unwrap() - 0.5).abs() < 1e-6);
    }

    #[tokio::test]
    async fn handle_routes_unknown_method_to_method_not_found() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[]);
        let runtime = make_runtime(PluginOutput::success(Bytes::new(), 0));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);
        let err = adapter
            .handle("tools/bogus", JsonValue::Null, &SessionContext::permissive())
            .await
            .expect_err("must error");
        assert!(matches!(err, McpError::MethodNotFound(_)));
    }

    #[tokio::test]
    async fn handle_routes_list_through_capability_adapter_trait() {
        let tmp = tempfile::tempdir().unwrap();
        let reg = make_registry_from_disk(&tmp, "kb", &[("search", "")]);
        let runtime = make_runtime(PluginOutput::success(Bytes::new(), 0));
        let adapter = ToolsAdapter::with_runtime(reg, runtime);
        assert_eq!(adapter.capability_name(), "tools");
        let value = adapter
            .handle("tools/list", JsonValue::Null, &SessionContext::permissive())
            .await
            .unwrap();
        let parsed: ListResult = serde_json::from_value(value).unwrap();
        assert_eq!(parsed.tools.len(), 1);
        assert_eq!(parsed.tools[0].name, "kb:search");
    }

    #[test]
    fn make_registry_helper_is_unreachable_only_on_purpose() {
        // The unused `make_registry` exists to document the
        // pub(crate) constraint. We won't call it; ensuring the
        // file still compiles is enough.
        let _ = make_registry;
    }

    #[test]
    fn encode_decode_round_trip() {
        let n = encode_tool_name("kb", "search");
        assert_eq!(n, "kb:search");
        assert_eq!(decode_tool_name(&n), Some(("kb", "search")));
        assert!(decode_tool_name("noop").is_none());
        assert!(decode_tool_name(":x").is_none());
        assert!(decode_tool_name("x:").is_none());
    }
}
