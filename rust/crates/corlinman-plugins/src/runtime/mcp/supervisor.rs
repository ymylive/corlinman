//! Crash-restart supervisor for MCP plugins.
//!
//! Lifecycle (design §Lifecycle, §"Failure handling"):
//!
//! ```text
//!   start_supervised ──▶ start_one ──▶ Initialized ──▶ wait_disconnect
//!         ▲                                                 │
//!         │            policy=on_crash / always              │ child exit
//!         └─ backoff ◀── crashes_in_window <= max ◀──────────┘
//!                            │
//!                            ▼ exceeds max
//!                          Failed (give up)
//! ```
//!
//! Backoff schedule mirrors `crate::supervisor::BACKOFF_SCHEDULE`
//! verbatim — `[1s, 2s, 5s, 10s]` capped at the last entry. The
//! crash-loop ceiling and window come from the manifest's `[mcp]`
//! table (`crash_loop_max`, `crash_loop_window_secs`) — defaults
//! match the existing supervisor (3 / 60s) so MCP plugins behave
//! identically to service plugins on the operator's dashboard.
//!
//! Test ergonomics: the watcher loop is parameterised on a
//! `BackoffPolicy` so unit tests can pass a `[10ms, 20ms]` schedule
//! and finish in < 1s. Production callers use [`default_backoff`].

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

use crate::manifest::RestartPolicy;
#[cfg(test)]
use crate::runtime::mcp::adapter::AdapterStatus;
use crate::runtime::mcp::adapter::McpAdapter;

/// Production backoff schedule. Identical to
/// `crate::supervisor::BACKOFF_SCHEDULE` — keep these in lockstep.
pub const DEFAULT_BACKOFF: &[Duration] = &[
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(5),
    Duration::from_secs(10),
];

/// Returns the production backoff schedule. Wrapper so callers can
/// pass `default_backoff()` to [`SupervisorPolicy::backoff`] without
/// importing the slice constant directly.
pub fn default_backoff() -> Vec<Duration> {
    DEFAULT_BACKOFF.to_vec()
}

/// Policy passed to the watcher loop. Tests inject a fast schedule
/// (`[10ms, 20ms]`) to keep unit tests under a second.
#[derive(Debug, Clone)]
pub struct SupervisorPolicy {
    /// Max crashes inside `window` before the watcher gives up.
    pub crash_loop_max: u32,
    /// Sliding crash-count window.
    pub window: Duration,
    /// Backoff schedule; the last entry is reused for subsequent
    /// retries past its end (matches existing supervisor semantics).
    pub backoff: Vec<Duration>,
    /// Whether crashes (any exit) should respawn (`Always`), only
    /// non-zero exits should respawn (`OnCrash`), or no respawn at
    /// all (`Never`). For C2 we don't currently distinguish exit
    /// status — a child that disconnects is treated as a crash for
    /// `OnCrash`. `Never` honours the design: no respawn, no log,
    /// just stop.
    pub restart_policy: RestartPolicy,
}

impl SupervisorPolicy {
    /// Build the production policy from a manifest's `[mcp]` block.
    pub fn from_manifest(cfg: &crate::manifest::McpConfig) -> Self {
        Self {
            crash_loop_max: cfg.crash_loop_max,
            window: Duration::from_secs(cfg.crash_loop_window_secs),
            backoff: default_backoff(),
            restart_policy: cfg.restart_policy,
        }
    }
}

/// Telemetry the watcher records for inspection. `restart_count` is
/// the number of successful respawns; `failed_at` carries the last
/// crash-loop reason if the watcher gave up. Tests assert against
/// this; production hooks tracing on top.
#[derive(Debug, Clone, Default)]
pub struct SupervisorStats {
    pub restart_count: u32,
    pub last_crash: Option<Instant>,
    /// `Some(reason)` once the watcher transitions the slot to
    /// terminal `Failed` (crash-loop ceiling) or terminates because
    /// `restart_policy = Never` and the child exited.
    pub failed_at: Option<String>,
    /// `true` after the watcher loop has resolved (clean stop).
    pub stopped: bool,
}

/// Handle returned by [`spawn_supervisor`]. Drop to let the watcher
/// keep running; call [`SupervisorHandle::stop`] to ask it to exit
/// cleanly. Reading [`SupervisorHandle::stats`] is cheap (an
/// `Arc<RwLock>` snapshot).
pub struct SupervisorHandle {
    cancel: CancellationToken,
    notify: Arc<Notify>,
    stats: Arc<tokio::sync::RwLock<SupervisorStats>>,
    join: Option<tokio::task::JoinHandle<()>>,
}

impl SupervisorHandle {
    /// Snapshot of the watcher's current stats.
    pub async fn stats(&self) -> SupervisorStats {
        self.stats.read().await.clone()
    }

    /// Ask the watcher to exit. Idempotent: a second call after the
    /// task has already completed is a no-op.
    pub async fn stop(&mut self) {
        self.cancel.cancel();
        self.notify.notify_waiters();
        if let Some(j) = self.join.take() {
            let _ = j.await;
        }
    }

    /// Wait for the watcher to terminate without explicitly cancelling.
    pub async fn join(&mut self) {
        if let Some(j) = self.join.take() {
            let _ = j.await;
        }
    }
}

impl Drop for SupervisorHandle {
    fn drop(&mut self) {
        // Best-effort: signal cancel + nudge the notify so the
        // watcher's wait_disconnect / sleep wake up on shutdown.
        self.cancel.cancel();
        self.notify.notify_waiters();
        if let Some(j) = self.join.take() {
            j.abort();
        }
    }
}

/// Spawn a watcher task that:
///   1. calls `adapter.start_one(name)` immediately if the slot isn't
///      already `Initialized` — this is the first-attempt boot,
///   2. waits for the live client to disconnect,
///   3. honours `restart_policy`:
///      `Never` marks `failed_at = "child exited; restart_policy=never"`
///      and returns; `OnCrash` / `Always` count the crash, apply the next
///      backoff entry, then respawn (modulo `crash_loop_max` ceiling).
///
/// Returns a [`SupervisorHandle`]; drop or `stop()` to end the loop.
pub async fn spawn_supervisor(
    adapter: Arc<McpAdapter>,
    name: String,
    policy: SupervisorPolicy,
) -> SupervisorHandle {
    let cancel = CancellationToken::new();
    let cancel_for_task = cancel.clone();
    let notify = Arc::new(Notify::new());
    let notify_for_task = Arc::clone(&notify);
    let stats: Arc<tokio::sync::RwLock<SupervisorStats>> =
        Arc::new(tokio::sync::RwLock::new(SupervisorStats::default()));
    let stats_for_task = Arc::clone(&stats);

    let join = tokio::spawn(async move {
        run_watcher(
            adapter,
            name,
            policy,
            cancel_for_task,
            notify_for_task,
            stats_for_task,
        )
        .await;
    });

    SupervisorHandle {
        cancel,
        notify,
        stats,
        join: Some(join),
    }
}

async fn run_watcher(
    adapter: Arc<McpAdapter>,
    name: String,
    policy: SupervisorPolicy,
    cancel: CancellationToken,
    nudge: Arc<Notify>,
    stats: Arc<tokio::sync::RwLock<SupervisorStats>>,
) {
    let mut crash_times: Vec<Instant> = Vec::new();
    let mut attempt: usize = 0;

    loop {
        if cancel.is_cancelled() {
            break;
        }

        // 1. Boot (or rebuild) the slot. start_one is idempotent on a
        // healthy slot — first iteration of the loop spawns; later
        // iterations spawn after a backoff.
        let start_result = adapter.start_one(&name).await;
        match start_result {
            Ok(()) => {
                attempt = 0;
            }
            Err(err) => {
                tracing::warn!(plugin = %name, error = %err, "MCP supervisor: start_one failed");
                let now = Instant::now();
                crash_times.retain(|t| now.duration_since(*t) <= policy.window);
                crash_times.push(now);
                stats.write().await.last_crash = Some(now);
                // start_one failure counts as a crash; fall through
                // to backoff branch.
                if matches!(policy.restart_policy, RestartPolicy::Never) {
                    let mut s = stats.write().await;
                    s.failed_at = Some(format!("start_one failed and restart_policy=never: {err}"));
                    s.stopped = true;
                    return;
                }
                if crash_times.len() as u32 > policy.crash_loop_max {
                    let mut s = stats.write().await;
                    s.failed_at = Some(format!(
                        "crash-looped: {} starts in {}s",
                        crash_times.len(),
                        policy.window.as_secs()
                    ));
                    s.stopped = true;
                    return;
                }
                let backoff = pick_backoff(&policy.backoff, attempt);
                attempt = attempt.saturating_add(1);
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    _ = nudge.notified() => continue,
                    _ = tokio::time::sleep(backoff) => continue,
                }
            }
        }

        // 2. We're up. Park on the live client's disconnect notify
        // (or cancel). We need to fish the client out of the slot;
        // the adapter doesn't expose it directly so we use the
        // status poll + `is_alive` cheap probe + a re-fetch trick.
        let client = match adapter.live_client_for_supervisor(&name).await {
            Ok(c) => c,
            Err(_) => {
                // Slot vanished (admin removed it) → exit cleanly.
                let mut s = stats.write().await;
                s.stopped = true;
                return;
            }
        };

        tokio::select! {
            _ = cancel.cancelled() => {
                // Operator asked us to stop; mark the slot Stopped
                // for symmetry with the `stop_one` admin path.
                let _ = adapter.stop_one(&name).await;
                let mut s = stats.write().await;
                s.stopped = true;
                return;
            }
            _ = client.wait_disconnect() => {
                // Child exited.
            }
        }

        // 3. Crash bookkeeping.
        let now = Instant::now();
        crash_times.retain(|t| now.duration_since(*t) <= policy.window);
        crash_times.push(now);
        {
            let mut s = stats.write().await;
            s.last_crash = Some(now);
        }

        tracing::warn!(
            plugin = %name,
            crashes_in_window = crash_times.len(),
            "MCP plugin child exited",
        );

        // 4. Restart policy gate.
        match policy.restart_policy {
            RestartPolicy::Never => {
                let mut s = stats.write().await;
                s.failed_at = Some("child exited; restart_policy=never".into());
                s.stopped = true;
                let _ = adapter.stop_one(&name).await;
                return;
            }
            RestartPolicy::OnCrash | RestartPolicy::Always => {
                // Both treat any disconnect as a crash for the
                // purposes of the watcher; an MCP child that exited
                // cleanly via stdin EOF (e.g. operator disable) goes
                // through `stop_one`, which cancels this task — it
                // never reaches here.
            }
        }

        // 5. Crash-loop ceiling.
        if crash_times.len() as u32 > policy.crash_loop_max {
            let mut s = stats.write().await;
            s.failed_at = Some(format!(
                "crash-looped: {} crashes in {}s",
                crash_times.len(),
                policy.window.as_secs()
            ));
            s.stopped = true;
            // Leave the adapter slot in Failed state. Don't call
            // stop_one — its current state already reflects the
            // disconnect (Failed).
            return;
        }

        // 6. Backoff before respawn.
        let backoff = pick_backoff(&policy.backoff, attempt);
        attempt = attempt.saturating_add(1);
        tokio::select! {
            _ = cancel.cancelled() => {
                let mut s = stats.write().await;
                s.stopped = true;
                return;
            }
            _ = nudge.notified() => {
                // Operator nudged: skip the wait.
            }
            _ = tokio::time::sleep(backoff) => {}
        }

        // 7. Respawn — loop back to step 1.
        {
            let mut s = stats.write().await;
            s.restart_count = s.restart_count.saturating_add(1);
        }
    }

    let mut s = stats.write().await;
    s.stopped = true;
}

/// Pick the backoff for `attempt`, capped at the last entry.
fn pick_backoff(schedule: &[Duration], attempt: usize) -> Duration {
    if schedule.is_empty() {
        return Duration::from_millis(50);
    }
    schedule
        .get(attempt)
        .copied()
        .unwrap_or_else(|| *schedule.last().unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{
        AllowlistMode, EntryPoint, EnvPassthrough, McpConfig, PluginManifest, PluginType,
        ResourcesAllowlist, ToolsAllowlist,
    };

    /// Same script as `adapter.rs` tests, copied here to keep the
    /// supervisor tests self-contained and avoid touching the
    /// adapter test harness.
    fn awk_responder() -> (String, Vec<String>) {
        let script = r#"awk '
            {
                line=$0
                m = match(line, /"id":[ ]*[0-9]+/)
                if (m == 0) {
                    m = match(line, /"id":[ ]*"[^"]*"/)
                }
                if (m == 0) { next }
                idstr = substr(line, RSTART+5, RLENGTH-5)
                gsub(/^[ ]+/, "", idstr)
                if (line ~ /"method"[ ]*:[ ]*"initialize"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"awk\",\"version\":\"0.0.1\"}}}\n", idstr
                    fflush()
                }
                else if (line ~ /"method"[ ]*:[ ]*"tools\/list"/) {
                    printf "{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"tools\":[]}}\n", idstr
                    fflush()
                }
            }'"#;
        ("sh".into(), vec!["-c".into(), script.into()])
    }

    /// Build a manifest with a custom restart policy + crash-loop
    /// ceiling. `handshake_ms` controls how long initialize gets.
    fn manifest(
        name: &str,
        command: &str,
        args: Vec<String>,
        restart_policy: RestartPolicy,
        crash_loop_max: u32,
        handshake_ms: u64,
    ) -> Arc<PluginManifest> {
        Arc::new(PluginManifest {
            manifest_version: 3,
            name: name.into(),
            version: "0.1.0".into(),
            description: String::new(),
            author: String::new(),
            plugin_type: PluginType::Mcp,
            entry_point: EntryPoint {
                command: command.into(),
                args,
                env: Default::default(),
            },
            communication: Default::default(),
            capabilities: Default::default(),
            sandbox: Default::default(),
            mcp: Some(McpConfig {
                autostart: false,
                restart_policy,
                crash_loop_max,
                crash_loop_window_secs: 60,
                handshake_timeout_ms: handshake_ms,
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

    fn fast_policy(restart_policy: RestartPolicy, crash_loop_max: u32) -> SupervisorPolicy {
        SupervisorPolicy {
            crash_loop_max,
            window: Duration::from_secs(60),
            backoff: vec![Duration::from_millis(20), Duration::from_millis(40)],
            restart_policy,
        }
    }

    /// Crash → respawn happy path: spawn an MCP server, kill the
    /// child, observe the supervisor respawns it. Asserts
    /// `stats.restart_count >= 1`.
    #[tokio::test]
    async fn crash_during_idle_respawns_with_backoff() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest(
            "crash-respawn",
            &cmd,
            args,
            RestartPolicy::OnCrash,
            5,
            5_000,
        );
        let adapter = Arc::new(McpAdapter::new());
        adapter
            .register(m.clone(), tmp.path().to_path_buf())
            .await
            .unwrap();

        let policy = fast_policy(RestartPolicy::OnCrash, 5);
        let mut sup = spawn_supervisor(Arc::clone(&adapter), "crash-respawn".into(), policy).await;

        // Wait for the first start to complete.
        for _ in 0..200 {
            if matches!(
                adapter.status("crash-respawn").await.unwrap(),
                AdapterStatus::Initialized
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(matches!(
            adapter.status("crash-respawn").await.unwrap(),
            AdapterStatus::Initialized
        ));

        // Force a "crash" by calling stop_one — this drops the
        // McpStdioClient inside the slot, which fires the
        // disconnect notify the watcher is parked on. The
        // supervisor sees the disconnect and respawns.
        //
        // (We can't easily SIGKILL the awk child from here without
        // pid-tracking, but a stop_one is wire-equivalent: same
        // wait_disconnect resolution, same OnCrash code path.)
        let _ = adapter.stop_one("crash-respawn").await;

        // Wait for restart_count to advance.
        let mut saw_restart = false;
        for _ in 0..400 {
            let s = sup.stats().await;
            if s.restart_count >= 1 {
                saw_restart = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(saw_restart, "expected a respawn within deadline");

        // Slot eventually re-initialises.
        for _ in 0..200 {
            if matches!(
                adapter.status("crash-respawn").await.unwrap(),
                AdapterStatus::Initialized
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }

        sup.stop().await;
    }

    /// `RestartPolicy::Never` honoured: a crash terminates the
    /// supervisor with `failed_at = Some("…restart_policy=never…")`.
    #[tokio::test]
    async fn restart_policy_never_does_not_respawn() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest("never-respawn", &cmd, args, RestartPolicy::Never, 5, 5_000);
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();

        let policy = fast_policy(RestartPolicy::Never, 5);
        let mut sup = spawn_supervisor(Arc::clone(&adapter), "never-respawn".into(), policy).await;

        // Wait for the first start.
        for _ in 0..200 {
            if matches!(
                adapter.status("never-respawn").await.unwrap(),
                AdapterStatus::Initialized
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }

        // Trigger crash.
        let _ = adapter.stop_one("never-respawn").await;

        // Wait for the supervisor to terminate.
        sup.join().await;
        let s = sup.stats().await;
        assert_eq!(s.restart_count, 0, "Never policy must not respawn");
        assert!(s.failed_at.is_some());
        let reason = s.failed_at.unwrap();
        assert!(
            reason.contains("never"),
            "expected restart_policy=never reason, got {reason:?}"
        );
        assert!(s.stopped);
    }

    /// Crash-loop ceiling honoured: a binary that always
    /// disconnects immediately gets respawned up to
    /// `crash_loop_max` times, then the watcher gives up and
    /// records `failed_at`.
    #[tokio::test]
    async fn crash_loop_max_stops_respawn() {
        if which::which("true").is_err() {
            eprintln!("`true` not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        // `true` exits immediately, no MCP handshake possible →
        // start_one always errors. The watcher counts that as a
        // crash.
        let m = manifest(
            "ever-broken",
            "true",
            vec![],
            RestartPolicy::OnCrash,
            2, // crash_loop_max = 2
            200,
        );
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();

        let policy = SupervisorPolicy {
            crash_loop_max: 2,
            window: Duration::from_secs(60),
            backoff: vec![Duration::from_millis(10), Duration::from_millis(10)],
            restart_policy: RestartPolicy::OnCrash,
        };
        let mut sup = spawn_supervisor(Arc::clone(&adapter), "ever-broken".into(), policy).await;

        sup.join().await;
        let s = sup.stats().await;
        assert!(s.failed_at.is_some(), "expected ceiling failure");
        let reason = s.failed_at.unwrap();
        assert!(
            reason.contains("crash-looped"),
            "expected crash-looped reason, got {reason:?}"
        );
        assert!(s.stopped);
    }

    /// Watcher honours `stop()`: ask it to exit, it stops promptly.
    #[tokio::test]
    async fn stop_terminates_watcher_within_deadline() {
        if which::which("awk").is_err() {
            eprintln!("awk not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (cmd, args) = awk_responder();
        let m = manifest("clean-stop", &cmd, args, RestartPolicy::OnCrash, 5, 5_000);
        let adapter = Arc::new(McpAdapter::new());
        adapter.register(m, tmp.path().to_path_buf()).await.unwrap();

        let policy = fast_policy(RestartPolicy::OnCrash, 5);
        let mut sup = spawn_supervisor(Arc::clone(&adapter), "clean-stop".into(), policy).await;

        // Wait for first start.
        for _ in 0..200 {
            if matches!(
                adapter.status("clean-stop").await.unwrap(),
                AdapterStatus::Initialized
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }

        // Stop the watcher; it should exit within ~200ms.
        let stopped = tokio::time::timeout(Duration::from_secs(3), sup.stop()).await;
        assert!(stopped.is_ok(), "supervisor.stop did not return in time");

        let s = sup.stats().await;
        assert!(s.stopped);
        // The watcher used the cancel path, so failed_at stays None.
        assert!(s.failed_at.is_none());
    }

    /// `pick_backoff` saturates at the last entry, never panics on
    /// out-of-bounds.
    #[test]
    fn pick_backoff_saturates() {
        let s = [
            Duration::from_secs(1),
            Duration::from_secs(2),
            Duration::from_secs(5),
        ];
        assert_eq!(pick_backoff(&s, 0), Duration::from_secs(1));
        assert_eq!(pick_backoff(&s, 2), Duration::from_secs(5));
        assert_eq!(pick_backoff(&s, 99), Duration::from_secs(5));
        assert_eq!(pick_backoff(&[], 0), Duration::from_millis(50));
    }
}
