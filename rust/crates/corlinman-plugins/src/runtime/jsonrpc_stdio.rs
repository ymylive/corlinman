//! JSON-RPC 2.0 over stdio runtime.
//!
//! Execution model (plan §7.3):
//!   1. spawn child with `cwd` = manifest dir, inherit env plus allowlisted
//!      extras.
//!   2. write a single `tools/call` request line to stdin and close it.
//!   3. read stdout line-by-line until one parses as a JSON-RPC response
//!      matching our `id`.
//!   4. translate `result` / `error` / `result.task_id` into `PluginOutput`.
//!
//! The same runtime handles both `sync` and `async` manifests — the only
//! difference is that `async` plugins may return `{"result":{"task_id":"..."}}`,
//! which we surface as `PluginOutput::AcceptedForLater`.

use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::time::{timeout, Duration};
use tokio_util::sync::CancellationToken;

use corlinman_core::metrics::{PLUGIN_EXECUTE_DURATION, PLUGIN_EXECUTE_TOTAL};
use corlinman_core::CorlinmanError;

use crate::manifest::PluginManifest;
use crate::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};
use crate::sandbox::{self, DockerRunner};

/// Default per-call deadline when neither manifest nor caller overrides it.
pub const DEFAULT_TIMEOUT_MS: u64 = 30_000;

/// Wire-level JSON-RPC 2.0 request (subset we actually emit).
#[derive(Debug, Serialize)]
struct JsonRpcRequest<'a> {
    jsonrpc: &'static str,
    id: u64,
    method: &'static str,
    params: CallParams<'a>,
}

#[derive(Debug, Serialize)]
struct CallParams<'a> {
    name: &'a str,
    arguments: serde_json::Value,
    session_key: &'a str,
    request_id: &'a str,
    trace_id: &'a str,
}

/// Wire-level JSON-RPC 2.0 response (subset we accept).
#[derive(Debug, Deserialize)]
struct JsonRpcResponse {
    #[serde(default)]
    jsonrpc: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    id: Option<serde_json::Value>,
    #[serde(default)]
    result: Option<serde_json::Value>,
    #[serde(default)]
    error: Option<JsonRpcError>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i64,
    message: String,
    #[serde(default)]
    #[allow(dead_code)]
    data: Option<serde_json::Value>,
}

/// Resolve the effective timeout: caller override > manifest value > default.
pub fn resolve_timeout(manifest: &PluginManifest, caller_override: Option<u64>) -> u64 {
    caller_override
        .or(manifest.communication.timeout_ms)
        .unwrap_or(DEFAULT_TIMEOUT_MS)
}

/// Stateless runtime adaptor implementing the [`PluginRuntime`] trait.
///
/// A single instance can be shared across all stdio plugins because every
/// call spawns a fresh child.
#[derive(Debug, Clone, Default)]
pub struct JsonRpcStdioRuntime;

#[async_trait]
impl PluginRuntime for JsonRpcStdioRuntime {
    async fn execute(
        &self,
        input: PluginInput,
        _progress: Option<Arc<dyn ProgressSink>>,
        cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError> {
        execute(
            &input.plugin,
            &input.tool,
            &input.cwd,
            // With no manifest handy, we only rely on caller's deadline hint.
            None,
            input.deadline_ms,
            &input.args_json,
            &input.session_key,
            &input.call_id,
            &input.trace_id,
            None,
            &input.env,
            cancel,
        )
        .await
    }

    fn kind(&self) -> &'static str {
        "jsonrpc_stdio"
    }
}

/// Low-level executor. Most callers should go through the trait; this entry
/// point is useful for the CLI `invoke` shim where we already have a
/// `PluginManifest` in hand.
#[allow(clippy::too_many_arguments)]
pub async fn execute(
    plugin_name: &str,
    tool_name: &str,
    cwd: &Path,
    manifest: Option<&PluginManifest>,
    timeout_override_ms: Option<u64>,
    arguments_json: &[u8],
    session_key: &str,
    request_id: &str,
    trace_id: &str,
    command_override: Option<(&str, &[String])>,
    env: &[(String, String)],
    cancel: CancellationToken,
) -> Result<PluginOutput, CorlinmanError> {
    execute_with_runner(
        plugin_name,
        tool_name,
        cwd,
        manifest,
        timeout_override_ms,
        arguments_json,
        session_key,
        request_id,
        trace_id,
        command_override,
        env,
        None,
        cancel,
    )
    .await
}

/// Classify a runtime outcome into a Prometheus `status` label value. Mirrors
/// the set documented next to `PLUGIN_EXECUTE_TOTAL`.
fn status_label(outcome: &Result<PluginOutput, CorlinmanError>) -> &'static str {
    match outcome {
        Ok(PluginOutput::Success { .. }) | Ok(PluginOutput::AcceptedForLater { .. }) => "ok",
        Ok(PluginOutput::Error { .. }) => "error",
        Err(CorlinmanError::Timeout { .. }) => "timeout",
        Err(CorlinmanError::Cancelled(_)) => "cancelled",
        Err(CorlinmanError::PluginRuntime { message, .. }) => {
            let lc = message.to_ascii_lowercase();
            if lc.contains("oom") || lc.contains("memory") {
                "oom"
            } else if lc.contains("denied") || lc.contains("permission") {
                "denied"
            } else {
                "error"
            }
        }
        Err(_) => "error",
    }
}

/// Like [`execute`], but accepts an injected `DockerRunner` so tests can
/// exercise the sandbox dispatch branch without a Docker daemon. When
/// `runner_override` is `None` and the manifest requests sandboxing, we lazily
/// build a real `DockerSandbox`.
///
/// S7.T3: every invocation records into
/// `corlinman_plugin_execute_duration_seconds{plugin}` and
/// `corlinman_plugin_execute_total{plugin, status}` — `status` derived from
/// the returned outcome via [`status_label`].
#[allow(clippy::too_many_arguments)]
pub async fn execute_with_runner(
    plugin_name: &str,
    tool_name: &str,
    cwd: &Path,
    manifest: Option<&PluginManifest>,
    timeout_override_ms: Option<u64>,
    arguments_json: &[u8],
    session_key: &str,
    request_id: &str,
    trace_id: &str,
    command_override: Option<(&str, &[String])>,
    env: &[(String, String)],
    runner_override: Option<Arc<dyn DockerRunner>>,
    cancel: CancellationToken,
) -> Result<PluginOutput, CorlinmanError> {
    let metric_start = Instant::now();
    let outcome = execute_with_runner_inner(
        plugin_name,
        tool_name,
        cwd,
        manifest,
        timeout_override_ms,
        arguments_json,
        session_key,
        request_id,
        trace_id,
        command_override,
        env,
        runner_override,
        cancel,
    )
    .await;
    let status = status_label(&outcome);
    let elapsed = metric_start.elapsed().as_secs_f64();
    PLUGIN_EXECUTE_DURATION
        .with_label_values(&[plugin_name])
        .observe(elapsed);
    PLUGIN_EXECUTE_TOTAL
        .with_label_values(&[plugin_name, status])
        .inc();
    outcome
}

#[allow(clippy::too_many_arguments)]
async fn execute_with_runner_inner(
    plugin_name: &str,
    tool_name: &str,
    cwd: &Path,
    manifest: Option<&PluginManifest>,
    timeout_override_ms: Option<u64>,
    arguments_json: &[u8],
    session_key: &str,
    request_id: &str,
    trace_id: &str,
    command_override: Option<(&str, &[String])>,
    env: &[(String, String)],
    runner_override: Option<Arc<dyn DockerRunner>>,
    cancel: CancellationToken,
) -> Result<PluginOutput, CorlinmanError> {
    // ---- resolve timeout + command -------------------------------------
    let timeout_ms = match manifest {
        Some(m) => resolve_timeout(m, timeout_override_ms),
        None => timeout_override_ms.unwrap_or(DEFAULT_TIMEOUT_MS),
    };
    let (program, args): (String, Vec<String>) = match (command_override, manifest) {
        (Some((p, a)), _) => (p.to_string(), a.to_vec()),
        (None, Some(m)) => (m.entry_point.command.clone(), m.entry_point.args.clone()),
        (None, None) => {
            return Err(CorlinmanError::PluginRuntime {
                plugin: plugin_name.to_string(),
                message: "no command given and no manifest".into(),
            });
        }
    };

    // ---- parse arguments into serde Value so we can embed it -----------
    let arguments_value: serde_json::Value = if arguments_json.is_empty() {
        serde_json::Value::Object(Default::default())
    } else {
        serde_json::from_slice(arguments_json).map_err(|e| CorlinmanError::Parse {
            what: "jsonrpc_stdio:arguments",
            message: e.to_string(),
        })?
    };

    let request = JsonRpcRequest {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: CallParams {
            name: tool_name,
            arguments: arguments_value,
            session_key,
            request_id,
            trace_id,
        },
    };
    let mut request_line = serde_json::to_vec(&request).map_err(|e| CorlinmanError::Parse {
        what: "jsonrpc_stdio:request_serialize",
        message: e.to_string(),
    })?;
    request_line.push(b'\n');

    // ---- sandbox branch: when the manifest requests containerisation ----
    if command_override.is_none() {
        if let Some(m) = manifest {
            if sandbox::is_enabled(&m.sandbox) {
                let runner = match runner_override {
                    Some(r) => r,
                    None => sandbox::docker::default_runner().await?,
                };
                return runner.run(m, &request_line, timeout_ms, cancel).await;
            }
        }
    }

    // ---- spawn child ----------------------------------------------------
    let mut cmd = Command::new(&program);
    cmd.args(&args)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (k, v) in env {
        cmd.env(k, v);
    }
    if let Some(m) = manifest {
        for (k, v) in &m.entry_point.env {
            cmd.env(k, v);
        }
    }

    let started = Instant::now();
    let mut child = cmd.spawn().map_err(CorlinmanError::from)?;
    let outcome = run_exchange(
        plugin_name,
        &mut child,
        &request_line,
        timeout_ms,
        cancel,
        started,
    )
    .await;

    // best-effort wait so we don't leave a zombie if the child exits promptly
    let _ = child.start_kill();
    let _ = child.wait().await;

    outcome
}

async fn run_exchange(
    plugin_name: &str,
    child: &mut Child,
    request_line: &[u8],
    timeout_ms: u64,
    cancel: CancellationToken,
    started: Instant,
) -> Result<PluginOutput, CorlinmanError> {
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| CorlinmanError::PluginRuntime {
            plugin: plugin_name.to_string(),
            message: "failed to capture child stdin".into(),
        })?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| CorlinmanError::PluginRuntime {
            plugin: plugin_name.to_string(),
            message: "failed to capture child stdout".into(),
        })?;

    let deadline = Duration::from_millis(timeout_ms);

    let fut = async move {
        stdin.write_all(request_line).await?;
        stdin.flush().await?;
        drop(stdin); // signal EOF so single-shot plugins can return

        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let bytes = reader.read_line(&mut line).await?;
        if bytes == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "plugin closed stdout before sending a response",
            ));
        }
        Ok::<String, std::io::Error>(line)
    };

    let line = tokio::select! {
        _ = cancel.cancelled() => {
            return Err(CorlinmanError::Cancelled("jsonrpc_stdio"));
        }
        r = timeout(deadline, fut) => match r {
            Err(_) => return Err(CorlinmanError::Timeout { what: "jsonrpc_stdio", millis: timeout_ms }),
            Ok(Err(e)) => return Err(CorlinmanError::PluginRuntime {
                plugin: plugin_name.to_string(),
                message: e.to_string(),
            }),
            Ok(Ok(line)) => line,
        }
    };

    let duration_ms = started.elapsed().as_millis() as u64;
    let trimmed = line.trim_end_matches(['\r', '\n']).trim();

    let resp: JsonRpcResponse =
        serde_json::from_str(trimmed).map_err(|e| CorlinmanError::Parse {
            what: "jsonrpc_stdio:response",
            message: format!("{e} (raw: {trimmed})"),
        })?;

    if let Some(v) = resp.jsonrpc.as_deref() {
        if v != "2.0" {
            return Err(CorlinmanError::Parse {
                what: "jsonrpc_stdio:response",
                message: format!("unexpected jsonrpc version {v}"),
            });
        }
    }

    if let Some(err) = resp.error {
        return Ok(PluginOutput::error(err.code, err.message, duration_ms));
    }

    let result = resp.result.unwrap_or(serde_json::Value::Null);

    // async: result.task_id (string) means "parked, await callback"
    if let Some(task_id) = result
        .as_object()
        .and_then(|o| o.get("task_id"))
        .and_then(|v| v.as_str())
    {
        return Ok(PluginOutput::AcceptedForLater {
            task_id: task_id.to_string(),
            duration_ms,
        });
    }

    let content = serde_json::to_vec(&result).map_err(|e| CorlinmanError::Parse {
        what: "jsonrpc_stdio:result_serialize",
        message: e.to_string(),
    })?;
    Ok(PluginOutput::success(Bytes::from(content), duration_ms))
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    use super::*;

    struct RecordingMockRunner {
        calls: AtomicUsize,
        last_request: Mutex<Vec<u8>>,
        canned: PluginOutput,
    }

    impl RecordingMockRunner {
        fn new(canned: PluginOutput) -> Arc<Self> {
            Arc::new(Self {
                calls: AtomicUsize::new(0),
                last_request: Mutex::new(Vec::new()),
                canned,
            })
        }
    }

    #[async_trait]
    impl DockerRunner for RecordingMockRunner {
        async fn run(
            &self,
            _manifest: &PluginManifest,
            request_line: &[u8],
            _timeout_ms: u64,
            _cancel: tokio_util::sync::CancellationToken,
        ) -> Result<PluginOutput, CorlinmanError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            *self.last_request.lock().unwrap() = request_line.to_vec();
            Ok(self.canned.clone())
        }
    }

    fn sandboxed_manifest() -> PluginManifest {
        PluginManifest {
            manifest_version: 2,
            name: "sbx".into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: crate::manifest::PluginType::Sync,
            entry_point: crate::manifest::EntryPoint {
                command: "python3".into(),
                args: vec!["main.py".into()],
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: crate::manifest::SandboxConfig {
                memory: Some("64m".into()),
                ..Default::default()
            },
            mcp: None,
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        }
    }

    fn bare_manifest() -> PluginManifest {
        PluginManifest {
            manifest_version: 2,
            name: "bare".into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: crate::manifest::PluginType::Sync,
            entry_point: crate::manifest::EntryPoint {
                command: "/definitely/not/here".into(),
                args: vec![],
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: None,
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        }
    }

    #[tokio::test]
    async fn sandbox_manifest_dispatches_to_runner() {
        let canned = PluginOutput::success(Bytes::from_static(br#"{"ok":true}"#), 12);
        let mock = RecordingMockRunner::new(canned);
        let m = sandboxed_manifest();

        let out = execute_with_runner(
            &m.name,
            "tool-x",
            std::path::Path::new("."),
            Some(&m),
            None,
            b"{\"hello\":\"world\"}",
            "sess",
            "req-1",
            "trace-1",
            None,
            &[],
            Some(mock.clone() as Arc<dyn DockerRunner>),
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .expect("runner path must succeed");

        assert_eq!(mock.calls.load(Ordering::SeqCst), 1);
        let recorded = mock.last_request.lock().unwrap().clone();
        let recorded = String::from_utf8(recorded).unwrap();
        assert!(recorded.contains("\"hello\":\"world\""), "args forwarded");
        assert!(recorded.ends_with('\n'), "line terminated");
        assert!(matches!(out, PluginOutput::Success { .. }));
    }

    #[tokio::test]
    async fn sandbox_runner_error_surfaces_unchanged() {
        let canned = PluginOutput::error(crate::sandbox::OOM_ERROR_CODE, "container OOM-killed", 7);
        let mock = RecordingMockRunner::new(canned);
        let m = sandboxed_manifest();

        let out = execute_with_runner(
            &m.name,
            "tool-x",
            std::path::Path::new("."),
            Some(&m),
            None,
            b"{}",
            "sess",
            "req-1",
            "trace-1",
            None,
            &[],
            Some(mock.clone() as Arc<dyn DockerRunner>),
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .expect("runner path must succeed");

        match out {
            PluginOutput::Error { code, .. } => {
                assert_eq!(code, crate::sandbox::OOM_ERROR_CODE);
            }
            other => panic!("expected Error, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn non_sandbox_manifest_takes_local_spawn_path() {
        // Point the manifest at a command that does not exist; the local
        // spawn path must surface an `io::Error`, which means the sandbox
        // dispatch did NOT fire (otherwise the mock would own the call).
        let canned = PluginOutput::success(Bytes::from_static(b"{}"), 0);
        let mock = RecordingMockRunner::new(canned);
        let m = bare_manifest();
        let err = execute_with_runner(
            &m.name,
            "tool-x",
            std::path::Path::new("."),
            Some(&m),
            Some(10),
            b"{}",
            "sess",
            "req-1",
            "trace-1",
            None,
            &[],
            Some(mock.clone() as Arc<dyn DockerRunner>),
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .expect_err("missing binary must fail spawn");
        assert!(matches!(err, CorlinmanError::Io(_)), "got {err:?}");
        assert_eq!(
            mock.calls.load(Ordering::SeqCst),
            0,
            "non-sandbox manifest must not touch the docker runner"
        );
    }

    #[test]
    fn resolve_timeout_prefers_override() {
        let mut m = PluginManifest {
            manifest_version: 2,
            name: "x".into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: crate::manifest::PluginType::Sync,
            entry_point: crate::manifest::EntryPoint {
                command: "true".into(),
                args: vec![],
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: None,
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        };
        m.communication.timeout_ms = Some(1234);
        assert_eq!(resolve_timeout(&m, None), 1234);
        assert_eq!(resolve_timeout(&m, Some(999)), 999);
        m.communication.timeout_ms = None;
        assert_eq!(resolve_timeout(&m, None), DEFAULT_TIMEOUT_MS);
    }

    // ---- S7.T3 metric wiring -----------------------------------------------

    #[test]
    fn status_label_classifies_outcomes() {
        use bytes::Bytes;

        let ok: Result<PluginOutput, CorlinmanError> =
            Ok(PluginOutput::success(Bytes::from_static(b"{}"), 1));
        assert_eq!(status_label(&ok), "ok");

        let err_plugin: Result<PluginOutput, CorlinmanError> =
            Ok(PluginOutput::error(-1, "boom", 1));
        assert_eq!(status_label(&err_plugin), "error");

        let pending: Result<PluginOutput, CorlinmanError> = Ok(PluginOutput::AcceptedForLater {
            task_id: "t".into(),
            duration_ms: 1,
        });
        assert_eq!(status_label(&pending), "ok");

        let timeout_err: Result<PluginOutput, CorlinmanError> = Err(CorlinmanError::Timeout {
            what: "x",
            millis: 100,
        });
        assert_eq!(status_label(&timeout_err), "timeout");

        let cancelled: Result<PluginOutput, CorlinmanError> = Err(CorlinmanError::Cancelled("x"));
        assert_eq!(status_label(&cancelled), "cancelled");

        let oom: Result<PluginOutput, CorlinmanError> = Err(CorlinmanError::PluginRuntime {
            plugin: "p".into(),
            message: "container killed (OOM)".into(),
        });
        assert_eq!(status_label(&oom), "oom");

        let denied: Result<PluginOutput, CorlinmanError> = Err(CorlinmanError::PluginRuntime {
            plugin: "p".into(),
            message: "permission denied".into(),
        });
        assert_eq!(status_label(&denied), "denied");
    }

    #[tokio::test]
    async fn execute_records_plugin_metrics() {
        use corlinman_core::metrics::{PLUGIN_EXECUTE_DURATION, PLUGIN_EXECUTE_TOTAL};

        // Use a unique plugin name so the snapshot is isolated across tests.
        let plugin = "metric_probe_plugin";
        let before = PLUGIN_EXECUTE_TOTAL
            .with_label_values(&[plugin, "error"])
            .get();
        let before_hist = PLUGIN_EXECUTE_DURATION
            .with_label_values(&[plugin])
            .get_sample_count();

        // Missing command + no manifest → error path. We don't care about the
        // specific error, only that the instrumenting wrapper ran.
        let _ = execute(
            plugin,
            "any",
            std::path::Path::new("."),
            None,
            Some(10),
            b"{}",
            "s",
            "r",
            "t",
            None,
            &[],
            CancellationToken::new(),
        )
        .await;

        let after = PLUGIN_EXECUTE_TOTAL
            .with_label_values(&[plugin, "error"])
            .get();
        let after_hist = PLUGIN_EXECUTE_DURATION
            .with_label_values(&[plugin])
            .get_sample_count();
        assert_eq!(
            after,
            before + 1.0,
            "execute should bump plugin_execute_total{{status=error}}"
        );
        assert_eq!(
            after_hist,
            before_hist + 1,
            "execute should observe plugin_execute_duration_seconds once"
        );
    }
}
