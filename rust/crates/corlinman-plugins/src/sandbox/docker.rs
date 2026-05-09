//! Docker sandbox via bollard — runs a single JSON-RPC exchange inside a
//! throwaway container shaped by `manifest.sandbox`.
//!
//! The entry point is the [`DockerRunner`] trait so `runtime::jsonrpc_stdio`
//! can dispatch through a boxed handle and tests can inject mocks without a
//! live Docker daemon. The concrete [`DockerSandbox`] builds a
//! [`bollard::models::HostConfig`] from the manifest, spawns a container from
//! a configurable base image (defaults to `corlinman-sandbox:latest`, which
//! `docker/Dockerfile.sandbox` produces), streams stdin/stdout over attach,
//! and cleans up with `auto_remove` + a force-remove RAII guard for the
//! panic / cancel paths.
//!
//! Design notes:
//!   - One container per invocation. A pool is an obvious follow-up, but
//!     container reuse needs careful stdin framing (mux / delimiter) that
//!     isn't in scope for M7.
//!   - `cmd` is `[entry_point.command, entry_point.args...]`. We do NOT
//!     interpret `$VAR`; the sandbox image is responsible for shell tools.
//!   - OOM detection uses `inspect_container` after `wait_container` (the
//!     wait stream only carries `status_code`; OOMKilled lives in `State`).

use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use bollard::container::{
    AttachContainerOptions, AttachContainerResults, Config, CreateContainerOptions, LogOutput,
    RemoveContainerOptions, WaitContainerOptions,
};
use bollard::models::HostConfig;
use bollard::Docker;
use bytes::{Bytes, BytesMut};
use futures::StreamExt;
use tokio::io::AsyncWriteExt;
use tokio::time::{timeout, Duration};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use corlinman_core::CorlinmanError;

use super::{is_enabled, parse_bytes, OOM_ERROR_CODE};
use crate::manifest::PluginManifest;
use crate::runtime::PluginOutput;

/// Default base image used when the manifest does not pin one explicitly.
/// `docker/Dockerfile.sandbox` is tagged with this name by CI.
pub const DEFAULT_SANDBOX_IMAGE: &str = "corlinman-sandbox:latest";

/// Abstraction over "run one JSON-RPC request inside a container".
///
/// `jsonrpc_stdio::execute` holds this as `Arc<dyn DockerRunner>`; in tests we
/// swap in a fake that returns canned `PluginOutput` values without touching
/// the Docker daemon.
#[async_trait]
pub trait DockerRunner: Send + Sync {
    /// Run `request_line` (already newline-terminated) against the sandbox
    /// image configured for this plugin. Returns the translated
    /// `PluginOutput`; upstream still performs JSON-RPC parsing.
    async fn run(
        &self,
        manifest: &PluginManifest,
        request_line: &[u8],
        timeout_ms: u64,
        cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError>;
}

/// Concrete `DockerRunner` that talks to the local Docker daemon via bollard.
#[derive(Clone)]
pub struct DockerSandbox {
    docker: Docker,
    default_image: String,
}

impl std::fmt::Debug for DockerSandbox {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DockerSandbox")
            .field("default_image", &self.default_image)
            .finish()
    }
}

impl DockerSandbox {
    /// Connect to the local Docker socket and ping the daemon.
    pub async fn new() -> Result<Self, CorlinmanError> {
        let docker = Docker::connect_with_socket_defaults()
            .map_err(|e| CorlinmanError::Internal(format!("docker connect: {e}")))?;
        docker
            .ping()
            .await
            .map_err(|e| CorlinmanError::Internal(format!("docker ping: {e}")))?;
        Ok(Self {
            docker,
            default_image: DEFAULT_SANDBOX_IMAGE.to_string(),
        })
    }

    /// Override the default image used when the manifest is silent.
    pub fn with_default_image(mut self, image: String) -> Self {
        self.default_image = image;
        self
    }

    /// Build the `HostConfig` slice of the container spec from the manifest's
    /// sandbox block. Exposed so tests can verify the mapping without
    /// spawning anything.
    pub fn host_config_from(manifest: &PluginManifest) -> Result<HostConfig, CorlinmanError> {
        let sb = &manifest.sandbox;
        let memory = match sb.memory.as_deref() {
            Some(s) => Some(parse_bytes(s)? as i64),
            None => None,
        };
        let nano_cpus = sb.cpus.map(|c| (c as f64 * 1e9) as i64);
        // Default to `none` when the manifest doesn't say — plugin code must
        // opt into network access explicitly.
        let network_mode = Some(sb.network.clone().unwrap_or_else(|| "none".to_string()));
        let binds = if sb.binds.is_empty() {
            None
        } else {
            Some(sb.binds.clone())
        };
        let cap_drop = if sb.cap_drop.is_empty() {
            None
        } else {
            Some(sb.cap_drop.clone())
        };
        Ok(HostConfig {
            memory,
            nano_cpus,
            readonly_rootfs: Some(sb.read_only_root),
            cap_drop,
            network_mode,
            binds,
            auto_remove: Some(true),
            ..Default::default()
        })
    }
}

/// RAII guard that force-removes a container on drop unless `disarm()` was
/// called. Keeps the cleanup path honest when cancellation / panics bypass
/// the happy path; docker's `auto_remove` can race here.
struct ContainerGuard {
    docker: Docker,
    id: String,
    disarmed: bool,
}

impl ContainerGuard {
    fn disarm(mut self) {
        self.disarmed = true;
    }
}

impl Drop for ContainerGuard {
    fn drop(&mut self) {
        if self.disarmed {
            return;
        }
        let docker = self.docker.clone();
        let id = std::mem::take(&mut self.id);
        tokio::spawn(async move {
            let _ = docker
                .remove_container(
                    &id,
                    Some(RemoveContainerOptions {
                        force: true,
                        ..Default::default()
                    }),
                )
                .await;
        });
    }
}

#[async_trait]
impl DockerRunner for DockerSandbox {
    async fn run(
        &self,
        manifest: &PluginManifest,
        request_line: &[u8],
        timeout_ms: u64,
        cancel: CancellationToken,
    ) -> Result<PluginOutput, CorlinmanError> {
        if !is_enabled(&manifest.sandbox) {
            return Err(CorlinmanError::Config(
                "DockerSandbox.run called on manifest without sandbox config".into(),
            ));
        }

        let host_config = Self::host_config_from(manifest)?;
        let image = self.default_image.clone();
        let mut cmd: Vec<String> = Vec::with_capacity(manifest.entry_point.args.len() + 1);
        cmd.push(manifest.entry_point.command.clone());
        cmd.extend(manifest.entry_point.args.iter().cloned());

        let env: Vec<String> = manifest
            .entry_point
            .env
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect();

        let config = Config {
            image: Some(image),
            cmd: Some(cmd),
            env: if env.is_empty() { None } else { Some(env) },
            attach_stdin: Some(true),
            attach_stdout: Some(true),
            attach_stderr: Some(true),
            open_stdin: Some(true),
            stdin_once: Some(true),
            tty: Some(false),
            working_dir: Some("/workspace".to_string()),
            host_config: Some(host_config),
            ..Default::default()
        };

        let name = format!("corlinman-{}-{}", manifest.name, Uuid::new_v4().simple());
        let create_opts = Some(CreateContainerOptions {
            name: name.clone(),
            platform: None,
        });

        let created = self
            .docker
            .create_container(create_opts, config)
            .await
            .map_err(|e| CorlinmanError::PluginRuntime {
                plugin: manifest.name.clone(),
                message: format!("docker create: {e}"),
            })?;
        let guard = ContainerGuard {
            docker: self.docker.clone(),
            id: created.id.clone(),
            disarmed: false,
        };

        let attach_opts = AttachContainerOptions::<String> {
            stdin: Some(true),
            stdout: Some(true),
            stderr: Some(true),
            stream: Some(true),
            logs: Some(false),
            detach_keys: None,
        };
        let attach = self
            .docker
            .attach_container(&created.id, Some(attach_opts))
            .await
            .map_err(|e| CorlinmanError::PluginRuntime {
                plugin: manifest.name.clone(),
                message: format!("docker attach: {e}"),
            })?;

        self.docker
            .start_container::<String>(&created.id, None)
            .await
            .map_err(|e| CorlinmanError::PluginRuntime {
                plugin: manifest.name.clone(),
                message: format!("docker start: {e}"),
            })?;

        let deadline = Duration::from_millis(timeout_ms.max(1));
        let started = Instant::now();
        let exchange = run_exchange(&manifest.name, attach, request_line, deadline);

        let response_bytes: Option<Bytes> = tokio::select! {
            _ = cancel.cancelled() => {
                // guard drops → force-remove
                return Err(CorlinmanError::Cancelled("sandbox_docker"));
            }
            r = exchange => match r {
                Err(e) => return Err(e),
                Ok(resp) => resp,
            }
        };

        // Wait for the container to exit so we can read OOMKilled / exit code.
        let wait_fut = async {
            let mut stream = self
                .docker
                .wait_container(&created.id, None::<WaitContainerOptions<String>>);
            let mut last: Option<i64> = None;
            while let Some(ev) = stream.next().await {
                match ev {
                    Ok(w) => last = Some(w.status_code),
                    Err(bollard::errors::Error::DockerContainerWaitError { code, .. }) => {
                        last = Some(code);
                        break;
                    }
                    Err(e) => {
                        return Err(CorlinmanError::PluginRuntime {
                            plugin: manifest.name.clone(),
                            message: format!("docker wait: {e}"),
                        });
                    }
                }
            }
            Ok::<Option<i64>, CorlinmanError>(last)
        };
        let exit_code = match timeout(deadline, wait_fut).await {
            Err(_) => {
                return Err(CorlinmanError::Timeout {
                    what: "sandbox_docker_wait",
                    millis: timeout_ms,
                });
            }
            Ok(r) => r?,
        };

        // OOM lives on State, not on the wait stream.
        let inspect = self.docker.inspect_container(&created.id, None).await.ok();
        let oom_killed = inspect
            .as_ref()
            .and_then(|c| c.state.as_ref())
            .and_then(|s| s.oom_killed)
            .unwrap_or(false);

        guard.disarm(); // auto_remove handles the happy path

        let duration_ms = started.elapsed().as_millis() as u64;

        if oom_killed {
            return Ok(PluginOutput::error(
                OOM_ERROR_CODE,
                "container OOM-killed",
                duration_ms,
            ));
        }

        match response_bytes {
            Some(line) => parse_response_line(&manifest.name, line, duration_ms),
            None => Err(CorlinmanError::PluginRuntime {
                plugin: manifest.name.clone(),
                message: format!(
                    "plugin closed stdout before responding (exit={:?})",
                    exit_code
                ),
            }),
        }
    }
}

/// Drive one stdin-write / stdout-readline exchange against an attached
/// container. Returns `Ok(Some(line))` when we got a JSON-RPC response line,
/// `Ok(None)` when stdout closed empty (caller surfaces an error).
async fn run_exchange(
    plugin: &str,
    mut attach: AttachContainerResults,
    request_line: &[u8],
    deadline: Duration,
) -> Result<Option<Bytes>, CorlinmanError> {
    let plugin_owned = plugin.to_string();
    let request_line = request_line.to_vec();
    let fut =
        async move {
            // 1. write stdin, close.
            attach.input.write_all(&request_line).await.map_err(|e| {
                CorlinmanError::PluginRuntime {
                    plugin: plugin_owned.clone(),
                    message: format!("stdin write: {e}"),
                }
            })?;
            attach
                .input
                .flush()
                .await
                .map_err(|e| CorlinmanError::PluginRuntime {
                    plugin: plugin_owned.clone(),
                    message: format!("stdin flush: {e}"),
                })?;
            attach
                .input
                .shutdown()
                .await
                .map_err(|e| CorlinmanError::PluginRuntime {
                    plugin: plugin_owned.clone(),
                    message: format!("stdin close: {e}"),
                })?;

            // 2. drain stdout until we see a full newline-terminated line (first
            //    one wins — the JSON-RPC response is a single line by contract).
            let mut buf = BytesMut::with_capacity(4096);
            let mut saw_newline = false;
            while let Some(chunk) = attach.output.next().await {
                match chunk {
                    Ok(LogOutput::StdOut { message }) | Ok(LogOutput::Console { message }) => {
                        buf.extend_from_slice(&message);
                        if message.contains(&b'\n') {
                            saw_newline = true;
                            break;
                        }
                    }
                    Ok(LogOutput::StdErr { .. }) | Ok(LogOutput::StdIn { .. }) => continue,
                    Err(e) => {
                        return Err(CorlinmanError::PluginRuntime {
                            plugin: plugin_owned.clone(),
                            message: format!("stdout stream: {e}"),
                        });
                    }
                }
            }
            if !saw_newline && buf.is_empty() {
                return Ok::<Option<Bytes>, CorlinmanError>(None);
            }
            // Trim at the first newline to keep strictly one line.
            let end = buf.iter().position(|&b| b == b'\n').unwrap_or(buf.len());
            Ok(Some(Bytes::copy_from_slice(&buf[..end])))
        };
    match timeout(deadline, fut).await {
        Err(_) => Err(CorlinmanError::Timeout {
            what: "sandbox_docker_io",
            millis: deadline.as_millis() as u64,
        }),
        Ok(r) => r,
    }
}

fn parse_response_line(
    plugin: &str,
    line: Bytes,
    duration_ms: u64,
) -> Result<PluginOutput, CorlinmanError> {
    #[derive(serde::Deserialize)]
    struct Resp {
        #[serde(default)]
        jsonrpc: Option<String>,
        #[serde(default)]
        result: Option<serde_json::Value>,
        #[serde(default)]
        error: Option<RespError>,
    }
    #[derive(serde::Deserialize)]
    struct RespError {
        code: i64,
        message: String,
    }

    let trimmed = std::str::from_utf8(&line)
        .map_err(|e| CorlinmanError::Parse {
            what: "sandbox_docker:response",
            message: e.to_string(),
        })?
        .trim();
    let resp: Resp = serde_json::from_str(trimmed).map_err(|e| CorlinmanError::Parse {
        what: "sandbox_docker:response",
        message: format!("{e} (raw: {trimmed})"),
    })?;
    if let Some(v) = resp.jsonrpc.as_deref() {
        if v != "2.0" {
            return Err(CorlinmanError::Parse {
                what: "sandbox_docker:response",
                message: format!("unexpected jsonrpc version {v}"),
            });
        }
    }
    if let Some(e) = resp.error {
        return Ok(PluginOutput::error(e.code, e.message, duration_ms));
    }
    let result = resp.result.unwrap_or(serde_json::Value::Null);
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
    let body = serde_json::to_vec(&result).map_err(|e| CorlinmanError::Parse {
        what: "sandbox_docker:result_serialize",
        message: e.to_string(),
    })?;
    let _ = plugin; // retained for symmetry / future structured logging
    Ok(PluginOutput::success(Bytes::from(body), duration_ms))
}

/// Resolve the runner to hand to `jsonrpc_stdio::execute` — kept as an
/// indirection so tests can override without touching Docker.
///
/// Call sites currently build a fresh `DockerSandbox` per `execute`. That's
/// acceptable for M7 because `Docker::connect_with_socket_defaults` is cheap
/// (just constructs a hyper client); if profiling ever says otherwise we
/// wire a shared runner into the runtime struct.
pub async fn default_runner() -> Result<Arc<dyn DockerRunner>, CorlinmanError> {
    Ok(Arc::new(DockerSandbox::new().await?))
}

// -----------------------------------------------------------------------------
// Unit tests (no Docker daemon)
// -----------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{EntryPoint, PluginType, SandboxConfig};

    fn fixture_manifest(sandbox: SandboxConfig) -> PluginManifest {
        PluginManifest {
            manifest_version: 2,
            name: "fixture".into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Sync,
            entry_point: EntryPoint {
                command: "python3".into(),
                args: vec!["main.py".into()],
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox,
            mcp: None,
            meta: None,
            protocols: vec!["openai_function".into()],
            hooks: vec![],
            skill_refs: vec![],
        }
    }

    #[test]
    fn host_config_from_maps_memory_and_cpus() {
        let sb = SandboxConfig {
            memory: Some("64m".into()),
            cpus: Some(0.5),
            read_only_root: true,
            cap_drop: vec!["ALL".into()],
            network: Some("none".into()),
            binds: vec!["/tmp/x:/mnt/x:ro".into()],
        };
        let m = fixture_manifest(sb);
        let host = DockerSandbox::host_config_from(&m).unwrap();
        assert_eq!(host.memory, Some(64 * 1024 * 1024));
        assert_eq!(host.nano_cpus, Some(500_000_000));
        assert_eq!(host.readonly_rootfs, Some(true));
        assert_eq!(host.cap_drop.as_deref(), Some(&["ALL".to_string()][..]));
        assert_eq!(host.network_mode.as_deref(), Some("none"));
        assert_eq!(
            host.binds.as_deref(),
            Some(&["/tmp/x:/mnt/x:ro".to_string()][..])
        );
        assert_eq!(host.auto_remove, Some(true));
    }

    #[test]
    fn host_config_defaults_network_to_none() {
        let sb = SandboxConfig {
            memory: Some("32m".into()),
            ..Default::default()
        };
        let m = fixture_manifest(sb);
        let host = DockerSandbox::host_config_from(&m).unwrap();
        assert_eq!(host.network_mode.as_deref(), Some("none"));
    }

    #[test]
    fn host_config_bad_memory_errors() {
        let sb = SandboxConfig {
            memory: Some("not-a-size".into()),
            ..Default::default()
        };
        let m = fixture_manifest(sb);
        assert!(matches!(
            DockerSandbox::host_config_from(&m),
            Err(CorlinmanError::Config(_))
        ));
    }
}
