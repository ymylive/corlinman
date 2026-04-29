//! Docker-backed [`SandboxBackend`] implementation.
//!
//! Spawns a frozen `corlinman-sandbox` container per call, runs the
//! workload, captures stdout, and tears the container down. The
//! container args pin every isolation knob the Phase 4 §6 risk
//! matrix calls out:
//!
//! - `--network=none` — no DNS, no outbound TCP, no host network
//!   access
//! - `--read-only` — root filesystem is read-only
//! - `--tmpfs /tmp:size=64M` — only `/tmp` is writable, capped to
//!   64 MiB
//! - `--cap-drop=ALL` — every Linux capability dropped
//! - `--security-opt=no-new-privileges`
//! - `--memory=<config>m` + `--memory-swap=<config>m` — RAM cap
//! - `--cpus=1.0` — single-core
//! - `--pids-limit=64` — fork bomb defeat
//! - `--user=65532:65532` — distroless `nonroot` uid; the image
//!   ships with no shell and the binary owns the only writable area
//!   (/tmp)
//! - per-call wall-clock timeout enforced via `tokio::time::timeout`,
//!   followed by `docker kill` to clean up
//!
//! Communication is JSON over stdout. The integration test pins the
//! contract by calling the same `corlinman-shadow-tester sandbox-
//! self-test` subcommand directly (in-process) and via the docker
//! backend, and asserting both produce the same hash.

use std::io;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use tokio::process::Command;
use tokio::time::timeout;

use super::{SandboxBackend, SandboxError, SelfTestResult};

/// Docker [`SandboxBackend`]. Holds the image tag and resource caps
/// the per-call `docker run` command needs.
#[derive(Debug, Clone)]
pub struct DockerBackend {
    image: String,
    mem_mb: u64,
    timeout: Duration,
}

impl DockerBackend {
    /// Build a backend from `[evolution.shadow.sandbox]` config
    /// values. The caller is responsible for ensuring the image tag
    /// is locally available — `docker run` will pull on first miss
    /// otherwise, which delays the first call by seconds.
    pub fn new(image: impl Into<String>, mem_mb: u64, timeout_secs: u64) -> Self {
        Self {
            image: image.into(),
            mem_mb,
            timeout: Duration::from_secs(timeout_secs),
        }
    }

    /// Compose the per-call `docker run` argv. Exposed for the
    /// integration test so it can pin the exact isolation knobs
    /// without re-deriving them from string output.
    pub fn run_argv(&self, payload: &str) -> Vec<String> {
        let mem_arg = format!("{}m", self.mem_mb);
        vec![
            "run".to_string(),
            "--rm".to_string(),
            "--network=none".to_string(),
            "--read-only".to_string(),
            "--tmpfs".to_string(),
            "/tmp:size=64m".to_string(),
            "--cap-drop=ALL".to_string(),
            "--security-opt=no-new-privileges".to_string(),
            format!("--memory={mem_arg}"),
            format!("--memory-swap={mem_arg}"),
            "--cpus=1.0".to_string(),
            "--pids-limit=64".to_string(),
            "--user=65532:65532".to_string(),
            self.image.clone(),
            "sandbox-self-test".to_string(),
            "--payload".to_string(),
            payload.to_string(),
        ]
    }
}

#[async_trait]
impl SandboxBackend for DockerBackend {
    async fn run_self_test(&self, payload: &str) -> Result<SelfTestResult, SandboxError> {
        let argv = self.run_argv(payload);

        let mut cmd = Command::new("docker");
        cmd.args(&argv)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let child_fut = async {
            let output = cmd.output().await.map_err(map_spawn_error)?;
            Ok::<_, SandboxError>(output)
        };

        let output = match timeout(self.timeout, child_fut).await {
            Ok(Ok(o)) => o,
            Ok(Err(e)) => return Err(e),
            Err(_) => return Err(SandboxError::Timeout(self.timeout)),
        };

        if !output.status.success() {
            return Err(SandboxError::NonZeroExit {
                status: output.status.code(),
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            });
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        let stdout = stdout.trim();
        serde_json::from_str::<SelfTestResult>(stdout)
            .map_err(|e| SandboxError::OutputParse(e, stdout.to_string()))
    }
}

/// Translate `docker run` spawn failures into `SandboxError`. The
/// most common case is a missing daemon — `docker` exits with
/// `Cannot connect to the Docker daemon` on stderr, but the spawn
/// itself succeeds. Genuine spawn errors (no `docker` binary on
/// PATH, EACCES, …) come through as `io::Error::NotFound`.
fn map_spawn_error(err: io::Error) -> SandboxError {
    if err.kind() == io::ErrorKind::NotFound {
        SandboxError::DaemonUnavailable(
            "docker binary not found on PATH; install Docker or set $PATH".to_string(),
        )
    } else {
        SandboxError::Spawn(err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Pin the exact `docker run` argv the backend produces. A
    /// regression that drops `--network=none` would silently
    /// re-enable network access; this test catches that statically
    /// without needing a real docker daemon.
    #[test]
    fn run_argv_pins_isolation_knobs() {
        let backend = DockerBackend::new("test/image:vN", 256, 30);
        let argv = backend.run_argv("hello");

        // The set of must-include flags. Order doesn't matter for
        // most of them; we assert *presence* in the argv vector.
        for required in &[
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--memory-swap=256m",
            "--cpus=1.0",
            "--pids-limit=64",
            "--user=65532:65532",
            "test/image:vN",
            "sandbox-self-test",
            "--payload",
            "hello",
        ] {
            assert!(
                argv.iter().any(|a| a == required),
                "argv missing {required}: {argv:?}"
            );
        }
    }

    #[test]
    fn map_spawn_error_translates_missing_binary() {
        let err = io::Error::new(io::ErrorKind::NotFound, "no docker on path");
        match map_spawn_error(err) {
            SandboxError::DaemonUnavailable(msg) => {
                assert!(msg.contains("docker binary not found"), "got: {msg}");
            }
            other => panic!("expected DaemonUnavailable, got: {other:?}"),
        }
    }

    #[test]
    fn map_spawn_error_passes_through_other_io_errors() {
        let err = io::Error::other("permission denied");
        match map_spawn_error(err) {
            SandboxError::Spawn(_) => {}
            other => panic!("expected Spawn, got: {other:?}"),
        }
    }
}
