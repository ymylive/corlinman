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

use corlinman_core::CorlinmanError;

use crate::manifest::PluginManifest;
use crate::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};

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
    use super::*;

    #[test]
    fn resolve_timeout_prefers_override() {
        let mut m = PluginManifest {
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
            meta: None,
        };
        m.communication.timeout_ms = Some(1234);
        assert_eq!(resolve_timeout(&m, None), 1234);
        assert_eq!(resolve_timeout(&m, Some(999)), 999);
        m.communication.timeout_ms = None;
        assert_eq!(resolve_timeout(&m, None), DEFAULT_TIMEOUT_MS);
    }
}
