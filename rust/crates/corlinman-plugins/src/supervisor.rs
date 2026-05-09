//! Service plugin supervisor — spawn, track, respawn (Sprint 2 T1).
//!
//! Responsibilities:
//!   1. On boot, for every `plugin_type = "service"` manifest the gateway
//!      holds, spawn the child process with a per-plugin UDS path exported
//!      via `CORLINMAN_PLUGIN_ADDR`.
//!   2. After spawn, hand the socket path back to the gateway so
//!      [`crate::runtime::service_grpc::ServiceRuntime::register`] can dial
//!      it and cache the tonic client.
//!   3. Run a watchdog task per plugin that observes child exits and
//!      respawns with exponential backoff (1s → 2s → 5s → 10s, capped).
//!   4. After three crashes inside 60 seconds, give up on restart and log
//!      an error so the operator investigates rather than spinning forever.
//!
//! The supervisor does **not** own the client cache — that lives in
//! `ServiceRuntime`. The split keeps gRPC dialing (which may block on the
//! plugin's boot latency) isolated from process lifecycle management.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use tokio::process::{Child, Command};
use tokio_util::sync::CancellationToken;

use corlinman_core::CorlinmanError;

use crate::manifest::PluginManifest;
use crate::runtime::service_grpc::{ServiceRuntime, PLUGIN_ADDR_ENV};

/// Max restart attempts inside the [`CRASH_LOOP_WINDOW`] before we stop
/// trying and emit a persistent error.
pub const MAX_RESTARTS_IN_WINDOW: u32 = 3;
/// Sliding window over which crash counts are evaluated.
pub const CRASH_LOOP_WINDOW: Duration = Duration::from_secs(60);
/// Backoff schedule, capped at the final entry for subsequent retries.
pub const BACKOFF_SCHEDULE: [Duration; 4] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(5),
    Duration::from_secs(10),
];

/// Tracked child process + its UDS path. `Child::kill_on_drop(true)` is
/// enforced by the spawn code so dropping the supervisor tears everything
/// down even on abrupt shutdown.
#[derive(Debug)]
pub struct PluginChild {
    pub process: Child,
    pub socket_path: PathBuf,
    pub last_restart: Instant,
}

/// Long-lived supervisor holding one child per service plugin.
///
/// Cheap to clone / wrap in `Arc` — the child map is a `DashMap`.
#[derive(Debug)]
pub struct PluginSupervisor {
    children: DashMap<String, PluginChild>,
    /// Directory under which per-plugin UDS files are created.
    /// Defaults to `/tmp/corlinman-plugins` in production.
    socket_root: PathBuf,
    /// Root cancellation token; watchdogs subscribe via `child_token`.
    shutdown: CancellationToken,
}

impl PluginSupervisor {
    /// Build a fresh supervisor. `socket_root` is created on demand.
    pub fn new(socket_root: PathBuf) -> Self {
        Self {
            children: DashMap::new(),
            socket_root,
            shutdown: CancellationToken::new(),
        }
    }

    /// Socket directory (exposed for tests).
    pub fn socket_root(&self) -> &Path {
        &self.socket_root
    }

    /// Number of currently-tracked children. Useful for dashboards / tests.
    pub fn child_count(&self) -> usize {
        self.children.len()
    }

    /// Spawn (or respawn) a service plugin and return the UDS path the
    /// gateway should dial. Caller is expected to invoke
    /// [`ServiceRuntime::register`] immediately after.
    pub async fn spawn_service(
        &self,
        manifest: &PluginManifest,
    ) -> Result<PathBuf, CorlinmanError> {
        // Ensure socket root exists before any child tries to bind.
        tokio::fs::create_dir_all(&self.socket_root)
            .await
            .map_err(|e| CorlinmanError::PluginRuntime {
                plugin: manifest.name.clone(),
                message: format!(
                    "failed to create socket root {}: {e}",
                    self.socket_root.display()
                ),
            })?;

        let socket_path = self.socket_root.join(format!("{}.sock", manifest.name));
        // Remove stale socket from a previous run or crashed child.
        if socket_path.exists() {
            let _ = tokio::fs::remove_file(&socket_path).await;
        }

        let mut cmd = Command::new(&manifest.entry_point.command);
        cmd.args(&manifest.entry_point.args)
            .env(PLUGIN_ADDR_ENV, &socket_path)
            .kill_on_drop(true);
        for (k, v) in &manifest.entry_point.env {
            cmd.env(k, v);
        }

        let child = cmd.spawn().map_err(|e| CorlinmanError::PluginRuntime {
            plugin: manifest.name.clone(),
            message: format!("spawn failed: {e}"),
        })?;

        let tracked = PluginChild {
            process: child,
            socket_path: socket_path.clone(),
            last_restart: Instant::now(),
        };
        self.children.insert(manifest.name.clone(), tracked);
        tracing::info!(
            plugin = manifest.name,
            socket = %socket_path.display(),
            "service plugin spawned",
        );

        Ok(socket_path)
    }

    /// Gracefully stop the child for `name`. The watchdog (if any) observes
    /// the exit through its owned `Child` reference so we don't race with it.
    /// We also trip `shutdown.cancel()` downstream by removing the entry —
    /// callers that want full supervisor teardown should call
    /// [`Self::shutdown`] instead.
    pub async fn stop_service(&self, name: &str) {
        if let Some((_, mut tracked)) = self.children.remove(name) {
            let _ = tracked.process.start_kill();
            let _ = tracked.process.wait().await;
            let _ = tokio::fs::remove_file(&tracked.socket_path).await;
            tracing::info!(plugin = name, "service plugin stopped");
        }
    }

    /// Tear down every tracked child; intended for gateway shutdown.
    pub async fn shutdown(&self) {
        self.shutdown.cancel();
        let names: Vec<String> = self.children.iter().map(|e| e.key().clone()).collect();
        for name in names {
            self.stop_service(&name).await;
        }
    }

    /// Spawn the per-plugin watchdog task. Must be called after the initial
    /// `spawn_service` + `ServiceRuntime::register` pair.
    ///
    /// The watchdog takes ownership of the child handle (via DashMap remove
    /// then re-insert on respawn) and runs until either the shutdown token
    /// fires or the plugin crosses the crash-loop threshold.
    pub fn start_watchdog(
        self: Arc<Self>,
        name: String,
        manifest: PluginManifest,
        runtime: Arc<ServiceRuntime>,
    ) {
        let shutdown = self.shutdown.child_token();
        tokio::spawn(async move {
            let mut crash_times: Vec<Instant> = Vec::new();
            let mut attempt: usize = 0;

            loop {
                // Take exclusive ownership of the Child handle so we can
                // `.wait()` on it without fighting other callers.
                let mut child = match self.children.remove(&name) {
                    Some((_, c)) => c,
                    None => {
                        tracing::debug!(plugin = %name, "watchdog: plugin already removed, exiting");
                        return;
                    }
                };

                let exit_status = tokio::select! {
                    _ = shutdown.cancelled() => {
                        let _ = child.process.start_kill();
                        let _ = child.process.wait().await;
                        let _ = tokio::fs::remove_file(&child.socket_path).await;
                        tracing::info!(plugin = %name, "watchdog: shutdown signal, exiting");
                        return;
                    }
                    status = child.process.wait() => status,
                };

                // Before respawn: drop the gRPC client so callers don't hit a
                // dangling channel, and clean up the stale socket.
                runtime.unregister(&name).await;
                let _ = tokio::fs::remove_file(&child.socket_path).await;

                let now = Instant::now();
                crash_times.retain(|t| now.duration_since(*t) <= CRASH_LOOP_WINDOW);
                crash_times.push(now);

                tracing::warn!(
                    plugin = %name,
                    ?exit_status,
                    crashes_in_window = crash_times.len(),
                    "service plugin exited",
                );

                if crash_times.len() as u32 > MAX_RESTARTS_IN_WINDOW {
                    tracing::error!(
                        plugin = %name,
                        window_secs = CRASH_LOOP_WINDOW.as_secs(),
                        "service plugin crash-looped; giving up on restart",
                    );
                    return;
                }

                // Backoff before respawn.
                let backoff = BACKOFF_SCHEDULE
                    .get(attempt)
                    .copied()
                    .unwrap_or(*BACKOFF_SCHEDULE.last().unwrap());
                attempt = attempt.saturating_add(1);

                tokio::select! {
                    _ = shutdown.cancelled() => {
                        tracing::info!(plugin = %name, "watchdog: shutdown during backoff, exiting");
                        return;
                    }
                    _ = tokio::time::sleep(backoff) => {}
                }

                match self.spawn_service(&manifest).await {
                    Ok(socket) => {
                        // Re-dial the child; on failure we drop the tracked
                        // entry so the next loop iteration will respawn.
                        if let Err(e) = runtime.register(&name, &socket).await {
                            tracing::error!(
                                plugin = %name,
                                error = %e,
                                "watchdog: re-register failed; will retry on next exit",
                            );
                            // The child exists in the map; force it to
                            // restart by killing so the loop continues.
                            if let Some(mut entry) = self.children.get_mut(&name) {
                                let _ = entry.process.start_kill();
                            }
                            continue;
                        }
                        tracing::info!(plugin = %name, "service plugin respawned");
                    }
                    Err(e) => {
                        tracing::error!(
                            plugin = %name,
                            error = %e,
                            "watchdog: spawn failed; backing off",
                        );
                        // Fall through to loop; `crash_times` will gate us
                        // out if we keep failing.
                    }
                }
            }
        });
    }
}

impl Drop for PluginSupervisor {
    fn drop(&mut self) {
        self.shutdown.cancel();
        // kill_on_drop on each Child handles the rest.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{EntryPoint, PluginType};

    fn fake_manifest(name: &str, command: &str, args: &[&str]) -> PluginManifest {
        PluginManifest {
            manifest_version: 2,
            name: name.into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Service,
            entry_point: EntryPoint {
                command: command.into(),
                args: args.iter().map(|s| s.to_string()).collect(),
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
    async fn spawn_missing_binary_returns_plugin_runtime_err() {
        let tmp = tempfile::tempdir().unwrap();
        let sup = PluginSupervisor::new(tmp.path().to_path_buf());
        let m = fake_manifest("ghost", "/nonexistent/binary/xyz-corlinman-test", &[]);
        let err = sup.spawn_service(&m).await.unwrap_err();
        match err {
            CorlinmanError::PluginRuntime { plugin, message } => {
                assert_eq!(plugin, "ghost");
                assert!(message.contains("spawn failed"), "got: {message}");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[tokio::test]
    async fn stop_on_unknown_plugin_is_noop() {
        let tmp = tempfile::tempdir().unwrap();
        let sup = PluginSupervisor::new(tmp.path().to_path_buf());
        // Should not panic or error.
        sup.stop_service("does-not-exist").await;
        assert_eq!(sup.child_count(), 0);
    }
}
