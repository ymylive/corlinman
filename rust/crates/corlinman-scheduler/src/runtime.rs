//! Tick loop that fires `[[scheduler.jobs]]` on schedule.
//!
//! Each job runs in its own `tokio::spawn` task: we sleep until the next
//! cron firing, dispatch the job's [`ActionSpec`], emit the matching
//! `EngineRun*` hook event, then loop. Cancellation flows through a shared
//! [`CancellationToken`]; pending sleeps and any in-flight subprocess wait
//! are dropped when the token cancels (the subprocess itself is killed via
//! `Command::kill_on_drop`).

use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use corlinman_core::config::SchedulerConfig;
use corlinman_hooks::{HookBus, HookEvent};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::cron::next_after;
use crate::jobs::{ActionSpec, JobSpec};
use crate::subprocess::{run_subprocess, SubprocessOutcome};

/// Handle to a running scheduler. Holds the per-job join handles so the
/// gateway shutdown path can `await` them (they exit cleanly when the
/// shared `CancellationToken` is cancelled).
pub struct SchedulerHandle {
    pub handles: Vec<JoinHandle<()>>,
}

impl SchedulerHandle {
    /// Convenience: drain every handle, ignoring join errors. Used by the
    /// gateway shutdown path; tests typically inspect handles directly.
    pub async fn join_all(self) {
        for h in self.handles {
            let _ = h.await;
        }
    }
}

/// Spawn one tick task per `[[scheduler.jobs]]` entry in `cfg`. Returns a
/// [`SchedulerHandle`] aggregating the per-job join handles.
///
/// Jobs whose cron expression fails to parse are dropped with a warning;
/// the rest of the scheduler continues. A config with zero parseable jobs
/// returns a handle with an empty `handles` vec (no-op scheduler).
pub fn spawn(
    cfg: &SchedulerConfig,
    bus: Arc<HookBus>,
    cancel: CancellationToken,
) -> SchedulerHandle {
    let mut handles = Vec::new();
    for job in &cfg.jobs {
        let Some(spec) = JobSpec::from_config(job) else {
            continue;
        };
        let bus = bus.clone();
        let cancel = cancel.clone();
        let handle = tokio::spawn(async move {
            run_job_loop(spec, bus, cancel).await;
        });
        handles.push(handle);
    }
    SchedulerHandle { handles }
}

/// Per-job tick loop. Responsibilities:
///
/// * compute the next firing time relative to `Utc::now()`;
/// * sleep until then or the cancel token fires (whichever comes first);
/// * dispatch on `ActionSpec` once awake;
/// * loop.
///
/// A schedule that never has another firing (cron expression valid but
/// astronomically improbable, e.g. Feb 30) breaks the loop with a warn —
/// we don't want to busy-spin asking for `next_after`.
async fn run_job_loop(spec: JobSpec, bus: Arc<HookBus>, cancel: CancellationToken) {
    tracing::info!(job = %spec.name, "scheduler: job loop started");
    loop {
        let now = Utc::now();
        let Some(next) = next_after(&spec.cron, now) else {
            tracing::warn!(
                job = %spec.name,
                "scheduler: cron has no upcoming firing; exiting job loop",
            );
            return;
        };
        let wait = (next - now)
            .to_std()
            .unwrap_or_else(|_| Duration::from_secs(1));
        tracing::debug!(
            job = %spec.name,
            next_fire_at = %next.to_rfc3339(),
            wait_secs = wait.as_secs(),
            "scheduler: next firing computed",
        );
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!(job = %spec.name, "scheduler: cancelled while sleeping; exiting");
                return;
            }
            _ = tokio::time::sleep(wait) => {}
        }
        // Cancel could have been signalled while sleep was in flight (the
        // race-y "sleep finished first" branch); re-check before firing
        // so we don't kick off a subprocess on the way down.
        if cancel.is_cancelled() {
            tracing::info!(job = %spec.name, "scheduler: cancelled before fire; exiting");
            return;
        }
        dispatch(&spec, bus.as_ref()).await;
    }
}

/// Run a single firing of `spec`. Public so the manual-trigger admin
/// endpoint can reuse it once we wire that up; for now it's only called
/// from the per-job tick loop above.
pub async fn dispatch(spec: &JobSpec, bus: &HookBus) {
    let run_id = Uuid::new_v4().to_string();
    match &spec.action {
        ActionSpec::Subprocess {
            command,
            args,
            timeout_secs,
            working_dir,
            env,
        } => {
            tracing::info!(
                job = %spec.name,
                run_id = %run_id,
                command = %command,
                "scheduler: subprocess job firing",
            );
            let outcome = run_subprocess(
                &spec.name,
                &run_id,
                command,
                args,
                *timeout_secs,
                working_dir.as_ref(),
                env,
            )
            .await;
            emit_outcome(bus, &spec.name, &run_id, outcome).await;
        }
        ActionSpec::RunAgent { .. } | ActionSpec::RunTool { .. } => {
            // Wave 2-B only wires Subprocess. The other actions surface
            // an `EngineRunFailed` so operators see the missing wiring
            // on the bus / `evolution_signals` instead of silent drops.
            tracing::warn!(
                job = %spec.name,
                run_id = %run_id,
                "scheduler: action kind not yet implemented; skipping fire",
            );
            let _ = bus
                .emit(HookEvent::EngineRunFailed {
                    run_id,
                    error_kind: "unsupported_action".into(),
                    exit_code: None,
                })
                .await;
        }
    }
}

/// Translate a [`SubprocessOutcome`] into the matching hook event and
/// emit it on the bus. Best-effort: bus-emit failures are logged but not
/// propagated, mirroring the rest of the gateway's "hooks never crash
/// the caller" stance.
async fn emit_outcome(bus: &HookBus, job: &str, run_id: &str, outcome: SubprocessOutcome) {
    let event = match outcome {
        SubprocessOutcome::Success { duration } => {
            tracing::info!(
                job = %job,
                run_id = %run_id,
                duration_ms = duration.as_millis() as u64,
                "scheduler: subprocess job completed",
            );
            HookEvent::EngineRunCompleted {
                run_id: run_id.to_string(),
                // Wave 2-B doesn't parse the engine's stdout for a count
                // yet; report 0 so the schema is honoured. A follow-up
                // wave can teach the runner to read a JSON summary off
                // the last stdout line and fill this in.
                proposals_generated: 0,
                duration_ms: duration.as_millis() as u64,
            }
        }
        SubprocessOutcome::NonZeroExit { code, duration } => {
            tracing::error!(
                job = %job,
                run_id = %run_id,
                exit_code = ?code,
                duration_ms = duration.as_millis() as u64,
                "scheduler: subprocess job exited non-zero",
            );
            HookEvent::EngineRunFailed {
                run_id: run_id.to_string(),
                error_kind: "exit_code".into(),
                exit_code: code,
            }
        }
        SubprocessOutcome::Timeout { duration } => {
            tracing::error!(
                job = %job,
                run_id = %run_id,
                duration_ms = duration.as_millis() as u64,
                "scheduler: subprocess job timed out",
            );
            HookEvent::EngineRunFailed {
                run_id: run_id.to_string(),
                error_kind: "timeout".into(),
                exit_code: None,
            }
        }
        SubprocessOutcome::SpawnFailed { error } => {
            tracing::error!(
                job = %job,
                run_id = %run_id,
                error = %error,
                "scheduler: subprocess job spawn failed",
            );
            HookEvent::EngineRunFailed {
                run_id: run_id.to_string(),
                error_kind: "spawn_failed".into(),
                exit_code: None,
            }
        }
    };
    if let Err(err) = bus.emit(event).await {
        tracing::warn!(error = %err, job = %job, run_id = %run_id, "scheduler: hook emit failed");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{JobAction, SchedulerJob};
    use corlinman_hooks::HookPriority;
    use std::collections::BTreeMap;

    /// Build a `JobSpec` with an arbitrary cron (we won't use the cron
    /// path in tests; we call `dispatch` directly to deterministically
    /// trigger one firing).
    fn spec_for(action: JobAction) -> JobSpec {
        let cfg = SchedulerJob {
            name: "unit".into(),
            cron: "0 0 0 * * * *".into(),
            timezone: None,
            action,
        };
        JobSpec::from_config(&cfg).expect("valid cron in test")
    }

    /// Receive the next event off a `HookSubscription`, with a small
    /// timeout so a hung dispatch doesn't deadlock the test binary.
    async fn next_event(sub: &mut corlinman_hooks::HookSubscription) -> HookEvent {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                match sub.recv().await {
                    Ok(e) => return e,
                    Err(corlinman_hooks::RecvError::Lagged(_)) => continue,
                    Err(corlinman_hooks::RecvError::Closed) => panic!("bus closed"),
                }
            }
        })
        .await
        .expect("event arrived within timeout")
    }

    #[tokio::test]
    async fn dispatch_subprocess_success_emits_completed() {
        let bus = Arc::new(HookBus::new(16));
        let mut sub = bus.subscribe(HookPriority::Normal);
        let spec = spec_for(JobAction::Subprocess {
            command: "true".into(),
            args: vec![],
            timeout_secs: 5,
            working_dir: None,
            env: BTreeMap::new(),
        });
        dispatch(&spec, bus.as_ref()).await;
        let evt = next_event(&mut sub).await;
        assert!(
            matches!(evt, HookEvent::EngineRunCompleted { .. }),
            "expected EngineRunCompleted, got {evt:?}",
        );
    }

    #[tokio::test]
    async fn dispatch_subprocess_failure_emits_failed_with_exit_code() {
        let bus = Arc::new(HookBus::new(16));
        let mut sub = bus.subscribe(HookPriority::Normal);
        let spec = spec_for(JobAction::Subprocess {
            command: "false".into(),
            args: vec![],
            timeout_secs: 5,
            working_dir: None,
            env: BTreeMap::new(),
        });
        dispatch(&spec, bus.as_ref()).await;
        let evt = next_event(&mut sub).await;
        match evt {
            HookEvent::EngineRunFailed {
                error_kind,
                exit_code,
                ..
            } => {
                assert_eq!(error_kind, "exit_code");
                assert_eq!(exit_code, Some(1));
            }
            other => panic!("expected EngineRunFailed, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn dispatch_subprocess_timeout_emits_failed_timeout() {
        let bus = Arc::new(HookBus::new(16));
        let mut sub = bus.subscribe(HookPriority::Normal);
        let spec = spec_for(JobAction::Subprocess {
            command: "sleep".into(),
            args: vec!["30".into()],
            timeout_secs: 1,
            working_dir: None,
            env: BTreeMap::new(),
        });
        dispatch(&spec, bus.as_ref()).await;
        let evt = next_event(&mut sub).await;
        match evt {
            HookEvent::EngineRunFailed { error_kind, .. } => {
                assert_eq!(error_kind, "timeout");
            }
            other => panic!("expected EngineRunFailed timeout, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn dispatch_subprocess_missing_binary_emits_spawn_failed() {
        let bus = Arc::new(HookBus::new(16));
        let mut sub = bus.subscribe(HookPriority::Normal);
        let spec = spec_for(JobAction::Subprocess {
            command: "/nonexistent/__corlinman_test_bin__".into(),
            args: vec![],
            timeout_secs: 5,
            working_dir: None,
            env: BTreeMap::new(),
        });
        dispatch(&spec, bus.as_ref()).await;
        let evt = next_event(&mut sub).await;
        match evt {
            HookEvent::EngineRunFailed { error_kind, .. } => {
                assert_eq!(error_kind, "spawn_failed");
            }
            other => panic!("expected EngineRunFailed spawn_failed, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn unsupported_action_emits_failed() {
        let bus = Arc::new(HookBus::new(16));
        let mut sub = bus.subscribe(HookPriority::Normal);
        let spec = spec_for(JobAction::RunAgent { prompt: "x".into() });
        dispatch(&spec, bus.as_ref()).await;
        let evt = next_event(&mut sub).await;
        match evt {
            HookEvent::EngineRunFailed { error_kind, .. } => {
                assert_eq!(error_kind, "unsupported_action");
            }
            other => panic!("expected EngineRunFailed unsupported_action, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn cancel_stops_job_loop_promptly() {
        // Use a cron that fires only once a year; we'd block forever
        // without the cancel path working.
        let bus = Arc::new(HookBus::new(16));
        let cancel = CancellationToken::new();
        let cfg = SchedulerConfig {
            jobs: vec![SchedulerJob {
                name: "yearly".into(),
                cron: "0 0 0 1 1 * *".into(), // 00:00:00 on Jan 1, any year
                timezone: None,
                action: JobAction::Subprocess {
                    command: "true".into(),
                    args: vec![],
                    timeout_secs: 5,
                    working_dir: None,
                    env: BTreeMap::new(),
                },
            }],
        };
        let handle = spawn(&cfg, bus.clone(), cancel.clone());
        // Let the loop park on `sleep`, then cancel.
        tokio::time::sleep(Duration::from_millis(50)).await;
        cancel.cancel();
        // Joining should complete near-instantly; bound to keep the
        // suite from hanging if cancellation regresses.
        tokio::time::timeout(Duration::from_secs(2), handle.join_all())
            .await
            .expect("handle joined after cancel");
    }
}
