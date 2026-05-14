//! `McpRuntime` — the [`PluginRuntime`] adapter that bridges
//! `McpAdapter::call_tool` into the existing `PluginOutput` ABI the
//! gateway dispatcher already speaks.
//!
//! Iter 7 scope (per design §"Adapter layer — translating MCP into the
//! corlinman tool ABI" and §"Implementation order — 10 iterations" item
//! 6/7): a thin runtime impl that:
//!
//!   1. Resolves the live `McpAdapter` for the input plugin name.
//!   2. Parses `PluginInput::args_json` as a `serde_json::Value` so it
//!      can become the MCP `arguments` field.
//!   3. Issues `tools/call` via the adapter, with a deadline derived
//!      from `PluginInput::deadline_ms` (falls back to the adapter's
//!      `handshake_timeout_ms * 6` default — same policy as
//!      `McpAdapter::call_tool`'s `None` branch).
//!   4. Projects [`mcp_tools::CallResult`] into [`PluginOutput`]:
//!        * `is_error == false` → `PluginOutput::Success { content: <flattened JSON of content> }`
//!        * `is_error == true`  → `PluginOutput::Error { code: -32603, message: <flattened text> }`
//!        * propagation of `AdapterError` (UnknownPlugin, UnknownTool,
//!          Handshake / Disconnected) → `CorlinmanError::PluginRuntime`
//!          so the chat dispatcher's existing error path lights up.
//!
//! Out of scope: the actual `chat.rs` dispatcher branch is the
//! gateway team's call-site, not ours. Wiring the new runtime into
//! `routes/chat.rs:561` is a one-line follow-up that ships with the
//! admin work in iter 8 / iter 9 of the design (i.e. lands when the
//! gateway gains an `AppState.mcp_adapter` field). Until then the
//! runtime is constructible standalone and unit-tested through its
//! [`PluginRuntime`] surface.
//!
//! ## Streaming
//!
//! MCP `notifications/progress` is a server-side notification with
//! no `id` — the existing `McpStdioClient` framing layer drops
//! notifications onto an internal channel that no one reads today
//! (iter 4-6 only consume request/response pairs). Hooking that
//! channel into [`ProgressSink`] is a Wave 4 follow-up; for iter 7
//! the `progress` argument is intentionally accepted-but-unused to
//! preserve the trait signature.
//!
//! ## Cancellation
//!
//! `cancel.cancelled()` races the call's deadline. If the cancel
//! fires first, we return `CorlinmanError::Cancelled` instead of
//! waiting for the upstream MCP server to finish. We do **not**
//! attempt to send an MCP `cancel` notification — the spec doesn't
//! require servers to honour one and many published servers ignore
//! it. The cleaner alternative (drop the in-flight oneshot, let the
//! response land in the void) is what the underlying client already
//! does on timeout.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use serde_json::Value as JsonValue;
use tokio_util::sync::CancellationToken;

use corlinman_core::CorlinmanError;

use crate::runtime::mcp::adapter::{AdapterError, McpAdapter};
use crate::runtime::mcp::client::ClientError;
use crate::runtime::mcp::schema::tools as mcp_tools;
use crate::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};

/// Default JSON-RPC error code we report when the upstream MCP server
/// flagged `is_error = true`. -32603 ("Internal error") is the closest
/// JSON-RPC code; servers that want a more specific code can encode
/// it inside the `text` content (we surface the verbatim text in
/// `message`).
const MCP_INTERNAL_ERROR_CODE: i64 = -32603;

/// `PluginRuntime` impl for MCP plugins. One instance per gateway —
/// holds an `Arc<McpAdapter>` so multiple chat dispatcher tasks can
/// execute against the same adapter concurrently.
#[derive(Clone)]
pub struct McpRuntime {
    adapter: Arc<McpAdapter>,
}

impl std::fmt::Debug for McpRuntime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("McpRuntime")
            .field("adapter", &"<McpAdapter>")
            .finish()
    }
}

impl McpRuntime {
    pub fn new(adapter: Arc<McpAdapter>) -> Self {
        Self { adapter }
    }

    /// Reference to the underlying adapter; useful for boot code that
    /// also needs to call `register` / `start_one`.
    pub fn adapter(&self) -> &Arc<McpAdapter> {
        &self.adapter
    }
}

#[async_trait]
impl PluginRuntime for McpRuntime {
    async fn execute(
        &self,
        input: PluginInput,
        _progress: Option<Arc<dyn ProgressSink>>,
        cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError> {
        let started = std::time::Instant::now();

        // 1. Decode args_json -> serde_json::Value. Fail-fast with a
        //    400-shaped CorlinmanError so the gateway returns a
        //    user-friendly error (rather than a panic on unwrap).
        let arguments: JsonValue = if input.args_json.is_empty() {
            JsonValue::Object(Default::default())
        } else {
            match serde_json::from_slice::<JsonValue>(&input.args_json) {
                Ok(v) => v,
                Err(err) => {
                    return Err(CorlinmanError::PluginRuntime {
                        plugin: input.plugin.clone(),
                        message: format!(
                            "args_json is not valid JSON for {}.{}: {err}",
                            input.plugin, input.tool
                        ),
                    });
                }
            }
        };

        let deadline = input.deadline_ms.map(Duration::from_millis);

        // 2. Race the call against the cancel token. The call itself
        //    enforces its own deadline; cancel is a separate cooperative
        //    short-circuit (e.g. client disconnect upstream).
        let call_fut = self
            .adapter
            .call_tool(&input.plugin, &input.tool, arguments, deadline);

        let result = tokio::select! {
            biased;
            _ = cancel.cancelled() => {
                return Err(CorlinmanError::PluginRuntime {
                    plugin: input.plugin.clone(),
                    message: format!(
                        "{}.{} cancelled before completion",
                        input.plugin, input.tool
                    ),
                });
            }
            r = call_fut => r,
        };

        let elapsed_ms = started.elapsed().as_millis() as u64;

        match result {
            Ok(call_result) => Ok(project_call_result(call_result, elapsed_ms)),
            Err(err) => Err(adapter_error_to_corlinman(err, &input.plugin)),
        }
    }

    fn kind(&self) -> &'static str {
        "mcp"
    }
}

/// Project an MCP `tools/call` result into a `PluginOutput`.
///
/// Success path: serialise the entire `CallResult` payload back to
/// JSON bytes. We deliberately preserve the upstream shape (content
/// array + isError flag) so downstream consumers see the same JSON a
/// direct MCP client would — the gateway dispatcher then surfaces
/// this verbatim in the `tools` event payload.
///
/// Error path: collapse the `content` array into a single message
/// string by joining all `Text` parts with newlines (non-text parts
/// are tagged in-line). The wire-level error code is fixed at
/// `MCP_INTERNAL_ERROR_CODE` because MCP doesn't reserve a tool-level
/// error namespace; the recoverable detail lives in the message.
pub(crate) fn project_call_result(call: mcp_tools::CallResult, duration_ms: u64) -> PluginOutput {
    if call.is_error {
        let message = flatten_content_to_message(&call.content);
        return PluginOutput::error(MCP_INTERNAL_ERROR_CODE, message, duration_ms);
    }

    // Re-serialise the full success result so callers can re-parse
    // either as the structured `CallResult` (mirrors `corlinman-mcp`)
    // or as the raw JSON object the rest of the plugin pipeline
    // already inspects via `serde_json::from_slice`.
    let payload = serde_json::to_vec(&call).unwrap_or_else(|err| {
        // CallResult is a fixed shape with no untyped serde::Value
        // fields that could fail; if we hit this branch there's a
        // genuine bug — fall back to an empty object so the dispatch
        // pipeline keeps moving rather than panicking the worker.
        tracing::error!(error = %err, "MCP CallResult re-serialise failed (programmer bug)");
        b"{}".to_vec()
    });
    PluginOutput::success(Bytes::from(payload), duration_ms)
}

fn flatten_content_to_message(content: &[mcp_tools::Content]) -> String {
    if content.is_empty() {
        return "MCP tool reported isError=true with no content".to_string();
    }
    let mut parts: Vec<String> = Vec::with_capacity(content.len());
    for c in content {
        match c {
            mcp_tools::Content::Text { text } => parts.push(text.clone()),
            mcp_tools::Content::Image { mime_type, .. } => {
                parts.push(format!("[image:{mime_type}]"))
            }
        }
    }
    parts.join("\n")
}

/// Translate an [`AdapterError`] into a [`CorlinmanError::PluginRuntime`].
///
/// We don't try to map the variant set 1:1 to JSON-RPC codes here; the
/// chat dispatcher already encodes `PluginRuntime` as a tool-call
/// failure with a free-text message (`tool_error_result(call_id,
/// -32603, msg)`). What matters is that the *cause* survives all the
/// way to the operator log line — `format!("{err}")` carries the
/// nested source via thiserror's `#[source]` chain.
pub(crate) fn adapter_error_to_corlinman(err: AdapterError, plugin: &str) -> CorlinmanError {
    let msg = match &err {
        AdapterError::UnknownPlugin(name) => {
            format!("MCP plugin {name:?} is not registered with the adapter")
        }
        AdapterError::UnknownTool { plugin, tool } => {
            format!(
                "tool {tool:?} is not exposed by MCP plugin {plugin:?} \
                 (filtered by tools_allowlist or never advertised)"
            )
        }
        AdapterError::NotMcpPlugin(name) => {
            format!("plugin {name:?} is not plugin_type = \"mcp\"")
        }
        AdapterError::MissingMcpConfig(name) => {
            format!("plugin {name:?} is missing the [mcp] manifest table")
        }
        AdapterError::Spawn { plugin, source } => {
            format!("MCP plugin {plugin:?} failed to spawn child process: {source}")
        }
        AdapterError::EnvPolicy { plugin, source } => {
            format!("MCP plugin {plugin:?} env_passthrough policy invalid: {source}")
        }
        AdapterError::Handshake { plugin, source } => match source {
            ClientError::Disconnected(reason) => {
                format!(
                    "MCP plugin {plugin:?} child not running: {reason} \
                     (start_one was never called or the child exited)"
                )
            }
            ClientError::Timeout { .. } => {
                format!("MCP plugin {plugin:?} handshake timed out")
            }
            other => format!("MCP plugin {plugin:?} handshake error: {other}"),
        },
        AdapterError::InvalidInitResult { plugin, message } => {
            format!("MCP plugin {plugin:?} returned malformed initialize result: {message}")
        }
        AdapterError::InvalidToolsListResult { plugin, message } => {
            format!("MCP plugin {plugin:?} returned malformed result: {message}")
        }
        AdapterError::Call {
            plugin,
            tool,
            source,
        } => match source {
            ClientError::Timeout { .. } => format!("MCP {plugin}.{tool} call timed out"),
            ClientError::Disconnected(reason) => {
                format!("MCP {plugin}.{tool} child disconnected: {reason}")
            }
            other => format!("MCP {plugin}.{tool} call failed: {other}"),
        },
        AdapterError::Disabled(name) => {
            format!("MCP plugin {name:?} is administratively disabled")
        }
        AdapterError::SentinelIo { plugin, message } => {
            format!("MCP plugin {plugin:?} sentinel I/O error: {message}")
        }
    };
    CorlinmanError::PluginRuntime {
        plugin: plugin.to_string(),
        message: msg,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{
        AllowlistMode, EntryPoint, EnvPassthrough, McpConfig, PluginManifest, PluginType,
        ResourcesAllowlist, RestartPolicy, ToolsAllowlist,
    };
    use crate::runtime::mcp::schema::tools::{CallResult, Content};
    use std::path::PathBuf;
    use std::sync::Arc;

    /// Re-use the iter 4 awk responder that returns echoed `id=…` text
    /// for any `tools/call`. Re-defined locally to keep the iter 7
    /// dispatch tests independent of the adapter test module.
    fn awk_responder() -> (&'static str, Vec<String>) {
        let script = r#"awk '
            {
                line=$0
                m = match(line, /"id":[ ]*[0-9]+/)
                if (m == 0) { m = match(line, /"id":[ ]*"[^"]*"/) }
                if (m == 0) { next }
                idstr = substr(line, RSTART+5, RLENGTH-5)
                gsub(/^[ ]+/, "", idstr)
                idtxt = idstr
                gsub(/"/, "", idtxt)
                if (line ~ /"method"[ ]*:[ ]*"initialize"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"awk-mcp\",\"version\":\"0.0.1\"}}}\n", idstr
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/list"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"tools\":[", idstr
                    printf "{\"name\":\"echo\",\"description\":\"echoes input\",\"inputSchema\":{\"type\":\"object\"}},"
                    printf "{\"name\":\"boom\",\"description\":\"\",\"inputSchema\":{\"type\":\"object\"}}"
                    printf "]}}\n"
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/call"/ && line ~ /"name"[ ]*:[ ]*"boom"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"kaboom: %s\"}],\"isError\":true}}\n", idstr, idtxt
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/call"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"id=%s\"}],\"isError\":false}}\n", idstr, idtxt
                    fflush()
                }
            }'"#;
        ("sh", vec!["-c".into(), script.into()])
    }

    fn manifest_for_dispatch(name: &str, command: &str, args: &[&str]) -> Arc<PluginManifest> {
        Arc::new(PluginManifest {
            manifest_version: 3,
            name: name.into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Mcp,
            entry_point: EntryPoint {
                command: command.into(),
                args: args.iter().map(|s| s.to_string()).collect(),
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: Some(McpConfig {
                autostart: false,
                restart_policy: RestartPolicy::OnCrash,
                crash_loop_max: 3,
                crash_loop_window_secs: 60,
                handshake_timeout_ms: 5_000,
                idle_shutdown_secs: 0,
                env_passthrough: EnvPassthrough {
                    allow: vec![],
                    deny: vec![],
                },
                tools_allowlist: ToolsAllowlist {
                    mode: AllowlistMode::All,
                    names: vec![],
                },
                resources_allowlist: ResourcesAllowlist::default(),
            }),
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        })
    }

    fn input_for(
        plugin: &str,
        tool: &str,
        args_json: &[u8],
        deadline_ms: Option<u64>,
    ) -> PluginInput {
        PluginInput {
            plugin: plugin.into(),
            tool: tool.into(),
            args_json: Bytes::copy_from_slice(args_json),
            call_id: format!("call-{tool}"),
            session_key: String::new(),
            trace_id: String::new(),
            cwd: PathBuf::from("."),
            env: Vec::new(),
            deadline_ms,
        }
    }

    #[test]
    fn project_call_result_success_round_trips() {
        let cr = CallResult {
            content: vec![Content::Text {
                text: "hello".into(),
            }],
            is_error: false,
        };
        let out = project_call_result(cr, 42);
        match out {
            PluginOutput::Success {
                content,
                duration_ms,
            } => {
                assert_eq!(duration_ms, 42);
                let parsed: serde_json::Value =
                    serde_json::from_slice(&content).expect("must round-trip JSON");
                assert_eq!(parsed["isError"], false);
                assert_eq!(parsed["content"][0]["type"], "text");
                assert_eq!(parsed["content"][0]["text"], "hello");
            }
            other => panic!("expected Success, got {other:?}"),
        }
    }

    #[test]
    fn project_call_result_error_collapses_text() {
        let cr = CallResult {
            content: vec![
                Content::Text {
                    text: "bad input".into(),
                },
                Content::Text {
                    text: "see docs".into(),
                },
            ],
            is_error: true,
        };
        let out = project_call_result(cr, 7);
        match out {
            PluginOutput::Error {
                code,
                message,
                duration_ms,
            } => {
                assert_eq!(code, MCP_INTERNAL_ERROR_CODE);
                assert_eq!(duration_ms, 7);
                assert_eq!(message, "bad input\nsee docs");
            }
            other => panic!("expected Error, got {other:?}"),
        }
    }

    #[test]
    fn project_call_result_error_image_falls_back_tag() {
        let cr = CallResult {
            content: vec![Content::Image {
                data: "deadbeef".into(),
                mime_type: "image/png".into(),
            }],
            is_error: true,
        };
        let out = project_call_result(cr, 0);
        match out {
            PluginOutput::Error { message, .. } => {
                assert_eq!(message, "[image:image/png]");
            }
            other => panic!("expected Error, got {other:?}"),
        }
    }

    #[test]
    fn project_call_result_error_empty_content_has_synthetic_message() {
        let cr = CallResult {
            content: vec![],
            is_error: true,
        };
        match project_call_result(cr, 0) {
            PluginOutput::Error { message, .. } => {
                assert!(message.contains("isError=true"));
            }
            other => panic!("expected Error, got {other:?}"),
        }
    }

    #[test]
    fn adapter_error_translates_unknown_plugin() {
        let err = adapter_error_to_corlinman(AdapterError::UnknownPlugin("ghost".into()), "ghost");
        match err {
            CorlinmanError::PluginRuntime { plugin, message } => {
                assert_eq!(plugin, "ghost");
                assert!(message.contains("not registered"));
            }
            other => panic!("expected PluginRuntime, got {other:?}"),
        }
    }

    #[test]
    fn adapter_error_translates_unknown_tool() {
        let err = adapter_error_to_corlinman(
            AdapterError::UnknownTool {
                plugin: "fs".into(),
                tool: "rm_rf".into(),
            },
            "fs",
        );
        match err {
            CorlinmanError::PluginRuntime { message, .. } => {
                assert!(message.contains("rm_rf"));
                assert!(message.contains("tools_allowlist"));
            }
            other => panic!("expected PluginRuntime, got {other:?}"),
        }
    }

    #[test]
    fn adapter_error_translates_handshake_disconnected() {
        let err = adapter_error_to_corlinman(
            AdapterError::Handshake {
                plugin: "fs".into(),
                source: ClientError::Disconnected("status=stopped".into()),
            },
            "fs",
        );
        match err {
            CorlinmanError::PluginRuntime { message, .. } => {
                assert!(message.contains("not running"));
                assert!(message.contains("status=stopped"));
            }
            other => panic!("expected PluginRuntime, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn execute_happy_path_returns_success_payload() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest_for_dispatch(
            "disp",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );

        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("disp").await.unwrap();

        let runtime = McpRuntime::new(adapter);
        let input = input_for("disp", "echo", b"{\"x\":1}", Some(2_000));
        let out = runtime
            .execute(input, None, CancellationToken::new())
            .await
            .expect("must succeed");
        match out {
            PluginOutput::Success { content, .. } => {
                let parsed: serde_json::Value = serde_json::from_slice(&content).unwrap();
                assert_eq!(parsed["isError"], false);
                let text = parsed["content"][0]["text"].as_str().unwrap();
                assert!(text.starts_with("id="), "unexpected: {text}");
            }
            other => panic!("expected Success, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn execute_error_path_returns_plugin_output_error() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest_for_dispatch(
            "disp-err",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );

        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("disp-err").await.unwrap();

        let runtime = McpRuntime::new(adapter);
        let input = input_for("disp-err", "boom", b"{}", Some(2_000));
        let out = runtime
            .execute(input, None, CancellationToken::new())
            .await
            .expect("call wire-level must still succeed");
        match out {
            PluginOutput::Error { code, message, .. } => {
                assert_eq!(code, MCP_INTERNAL_ERROR_CODE);
                assert!(
                    message.starts_with("kaboom:"),
                    "unexpected error msg: {message}"
                );
            }
            other => panic!("expected Error, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn execute_unknown_plugin_returns_corlinman_error() {
        let runtime = McpRuntime::new(Arc::new(McpAdapter::new()));
        let input = input_for("nope", "x", b"{}", Some(500));
        let err = runtime
            .execute(input, None, CancellationToken::new())
            .await
            .expect_err("unknown plugin must error");
        match err {
            CorlinmanError::PluginRuntime { plugin, message } => {
                assert_eq!(plugin, "nope");
                assert!(message.contains("not registered"));
            }
            other => panic!("expected PluginRuntime, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn execute_invalid_args_json_returns_corlinman_error() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest_for_dispatch(
            "disp-bad-args",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );

        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("disp-bad-args").await.unwrap();

        let runtime = McpRuntime::new(adapter);
        let input = input_for("disp-bad-args", "echo", b"not-json", Some(2_000));
        let err = runtime
            .execute(input, None, CancellationToken::new())
            .await
            .expect_err("bad args must error");
        match err {
            CorlinmanError::PluginRuntime { message, .. } => {
                assert!(message.contains("not valid JSON"));
            }
            other => panic!("expected PluginRuntime, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn execute_empty_args_json_treated_as_empty_object() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest_for_dispatch(
            "disp-empty",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );

        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();
        adapter.start_one("disp-empty").await.unwrap();

        let runtime = McpRuntime::new(adapter);
        let input = input_for("disp-empty", "echo", b"", Some(2_000));
        runtime
            .execute(input, None, CancellationToken::new())
            .await
            .expect("empty args_json must default to {}");
    }

    #[tokio::test]
    async fn execute_cancelled_before_completion_returns_error() {
        // Use a sleeping child that never replies, so the cancel token
        // is the only thing that can release the future.
        if which::which("sleep").is_err() {
            eprintln!("sleep not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let m = manifest_for_dispatch("disp-cancel", "sleep", &["10"]);
        // Drop the handshake budget so register/start_one fail fast,
        // then we test the cancel path against an unstarted slot —
        // which still goes through the cancel arm because
        // call_tool's status check raises Disconnected after select.
        // Easier path: spawn awk so handshake succeeds, then call a
        // tool whose name is allowlisted — but use a cancel token
        // that's pre-cancelled.
        let _ = m;
        let (cmd, args) = awk_responder();
        let m2 = manifest_for_dispatch(
            "disp-cancel2",
            cmd,
            &args.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
        );

        let adapter = Arc::new(McpAdapter::new());
        adapter
            .register(m2, tmp.path().to_path_buf())
            .await
            .unwrap();
        adapter.start_one("disp-cancel2").await.unwrap();

        let runtime = McpRuntime::new(adapter);
        let cancel = CancellationToken::new();
        cancel.cancel(); // pre-cancelled

        let input = input_for("disp-cancel2", "echo", b"{}", Some(5_000));
        let err = runtime
            .execute(input, None, cancel)
            .await
            .expect_err("pre-cancelled must short-circuit");
        match err {
            CorlinmanError::PluginRuntime { message, .. } => {
                assert!(message.contains("cancelled"));
            }
            other => panic!("expected PluginRuntime cancelled, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn runtime_kind_label_is_mcp() {
        let runtime = McpRuntime::new(Arc::new(McpAdapter::new()));
        assert_eq!(runtime.kind(), "mcp");
    }
}
