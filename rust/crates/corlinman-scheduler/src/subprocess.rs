//! Spawn a child process for a `JobAction::Subprocess` job and report the
//! outcome. Keeps the OS-level details (pipe wiring, timeout-kill, line
//! split) out of `runtime.rs` so the tick loop reads top-down.
//!
//! Behaviour:
//!
//! * `stdout` / `stderr` are piped and forwarded to `tracing` line-by-line:
//!   stdout at `info`, stderr at `warn`. Each line carries the job name +
//!   `run_id` as fields so multiple concurrent jobs are distinguishable in
//!   logs.
//! * `tokio::time::timeout` wraps the `child.wait()`. On expiry we send the
//!   child a SIGKILL (via `Child::start_kill` + best-effort `wait`) and
//!   return [`SubprocessOutcome::Timeout`]. The strong kill is deliberate:
//!   the engine sometimes blocks on RPCs and a SIGTERM grace period would
//!   let it linger past the schedule's next firing.
//! * If `Command::spawn` itself fails (binary not on PATH, working dir
//!   missing, permission denied, ...) we return [`SubprocessOutcome::SpawnFailed`]
//!   so the caller can emit `EngineRunFailed { error_kind: "spawn_failed" }`
//!   without the gateway crashing.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::time::Instant;

/// Outcome of a single subprocess run. Kept enum-shaped so callers can
/// match on the failure mode and pick the right `error_kind` string for
/// the `EngineRunFailed` hook event.
#[derive(Debug)]
pub enum SubprocessOutcome {
    /// Child exited 0 within the timeout. `duration` is wall-clock from
    /// before-spawn until exit.
    Success { duration: Duration },
    /// Child exited non-zero within the timeout.
    NonZeroExit {
        code: Option<i32>,
        duration: Duration,
    },
    /// Timeout elapsed. The child has been signalled with SIGKILL by the
    /// time we return; `duration` carries the timeout we hit.
    Timeout { duration: Duration },
    /// `Command::spawn` failed before we ever had a `Child`.
    SpawnFailed { error: String },
}

/// Spawn `command args` and wait up to `timeout_secs` for it. The `job`
/// + `run_id` strings are only used to tag the per-line tracing forwarders
///   so the caller can correlate subprocess output to the firing record.
pub async fn run_subprocess(
    job: &str,
    run_id: &str,
    command: &str,
    args: &[String],
    timeout_secs: u64,
    working_dir: Option<&PathBuf>,
    env: &BTreeMap<String, String>,
) -> SubprocessOutcome {
    let started = Instant::now();
    let mut cmd = Command::new(command);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(dir) = working_dir {
        cmd.current_dir(dir);
    }
    for (k, v) in env {
        cmd.env(k, v);
    }

    let mut child: Child = match cmd.spawn() {
        Ok(c) => c,
        Err(err) => {
            tracing::error!(
                job = %job,
                run_id = %run_id,
                command = %command,
                error = %err,
                "scheduler: subprocess spawn failed",
            );
            return SubprocessOutcome::SpawnFailed {
                error: err.to_string(),
            };
        }
    };

    // Forward stdout / stderr line-by-line into tracing. `take()` is safe
    // because we configured `piped()` above. The two forwarder tasks
    // hand-roll their own end-of-stream detection so they exit cleanly
    // when the child closes the pipe.
    if let Some(out) = child.stdout.take() {
        let job = job.to_string();
        let run_id = run_id.to_string();
        tokio::spawn(async move {
            let mut lines = BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                tracing::info!(job = %job, run_id = %run_id, stream = "stdout", "{}", line);
            }
        });
    }
    if let Some(err) = child.stderr.take() {
        let job = job.to_string();
        let run_id = run_id.to_string();
        tokio::spawn(async move {
            let mut lines = BufReader::new(err).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                tracing::warn!(job = %job, run_id = %run_id, stream = "stderr", "{}", line);
            }
        });
    }

    let timeout = Duration::from_secs(timeout_secs.max(1));
    let wait = tokio::time::timeout(timeout, child.wait()).await;
    match wait {
        Ok(Ok(status)) => {
            let elapsed = started.elapsed();
            if status.success() {
                SubprocessOutcome::Success { duration: elapsed }
            } else {
                SubprocessOutcome::NonZeroExit {
                    code: status.code(),
                    duration: elapsed,
                }
            }
        }
        Ok(Err(err)) => {
            // `Child::wait` itself failed (rare; usually means the OS
            // could not reap). Treat as non-zero exit with no code so the
            // observer still records the run as failed.
            tracing::error!(
                job = %job,
                run_id = %run_id,
                error = %err,
                "scheduler: child.wait() failed",
            );
            SubprocessOutcome::NonZeroExit {
                code: None,
                duration: started.elapsed(),
            }
        }
        Err(_) => {
            // Timeout. SIGKILL the child; ignore the kill error (process
            // may have just exited).
            tracing::error!(
                job = %job,
                run_id = %run_id,
                timeout_secs = timeout_secs,
                "scheduler: subprocess timed out; sending SIGKILL",
            );
            let _ = child.start_kill();
            // Reap so the OS releases the slot. Bound the post-kill wait
            // so a wedged kernel can't park us forever.
            let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
            SubprocessOutcome::Timeout { duration: timeout }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn success_on_zero_exit() {
        let outcome = run_subprocess("test", "run-1", "true", &[], 5, None, &BTreeMap::new()).await;
        assert!(matches!(outcome, SubprocessOutcome::Success { .. }));
    }

    #[tokio::test]
    async fn non_zero_on_false() {
        let outcome =
            run_subprocess("test", "run-2", "false", &[], 5, None, &BTreeMap::new()).await;
        match outcome {
            SubprocessOutcome::NonZeroExit { code, .. } => {
                // POSIX `false` exits 1; on macOS/Linux this is stable.
                assert_eq!(code, Some(1));
            }
            other => panic!("expected NonZeroExit, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn timeout_kills_long_runner() {
        // `sleep 30` would happily outlive the test; a 1s timeout must
        // cut it short.
        let outcome = run_subprocess(
            "test",
            "run-3",
            "sleep",
            &["30".into()],
            1,
            None,
            &BTreeMap::new(),
        )
        .await;
        assert!(matches!(outcome, SubprocessOutcome::Timeout { .. }));
    }

    #[tokio::test]
    async fn spawn_failed_for_missing_binary() {
        let outcome = run_subprocess(
            "test",
            "run-4",
            "/nonexistent/__corlinman_test__",
            &[],
            5,
            None,
            &BTreeMap::new(),
        )
        .await;
        assert!(matches!(outcome, SubprocessOutcome::SpawnFailed { .. }));
    }

    #[tokio::test]
    async fn env_is_passed_to_child() {
        // `sh -c 'test "$FOO" = bar'` exits 0 iff FOO is "bar".
        let mut env = BTreeMap::new();
        env.insert("FOO".into(), "bar".into());
        let outcome = run_subprocess(
            "test",
            "run-5",
            "sh",
            &["-c".into(), "test \"$FOO\" = bar".into()],
            5,
            None,
            &env,
        )
        .await;
        assert!(
            matches!(outcome, SubprocessOutcome::Success { .. }),
            "child should see FOO=bar; got {outcome:?}"
        );
    }
}
