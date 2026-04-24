//! Periodic cleanup of rotated gateway log files.
//!
//! The `tracing-appender` [`RollingFileAppender`](tracing_appender::rolling::RollingFileAppender)
//! rolls the file on wall-clock boundaries (daily / hourly / ...) but it
//! never deletes old files — that is the operator's job. This module
//! spawns a `tokio::task` that wakes once an hour, scans the parent
//! directory of the active log file, and removes any rotated sibling
//! whose `mtime` is older than `retention_days`.
//!
//! # Matching
//!
//! Rolling appender file names have the shape `<prefix>.<date-suffix>`
//! where the suffix is `YYYY-MM-DD` (daily), `YYYY-MM-DD-HH` (hourly),
//! or `YYYY-MM-DD-HH-mm` (minutely). We compile a permissive regex
//! covering all three so operators can change `rotation` at runtime
//! without orphaning files written under the old cadence.
//!
//! The **active** file (bare `<prefix>` with no suffix, only used with
//! `Rotation::NEVER`) is never deleted regardless of mtime.
//!
//! # Failure mode
//!
//! Every IO error is warn-and-continue. A broken pass doesn't stop the
//! task — it'll try again on the next tick.

use std::path::{Path, PathBuf};
use std::time::Duration;

use regex::Regex;
use tokio_util::sync::CancellationToken;

use corlinman_core::metrics::LOG_FILES_REMOVED;

/// How often the retention sweep runs. Exposed as a `const` so tests can
/// reference it without reaching into the task's internals.
pub const SWEEP_INTERVAL: Duration = Duration::from_secs(3600);

/// Spawn the retention task. Returns the `JoinHandle` so `main` can
/// optionally await it on shutdown (the task exits when `cancel` fires).
///
/// `retention_days = 0` disables deletion — the task still runs but
/// every candidate is skipped. This keeps the boot path uniform (always
/// spawn) while giving operators an explicit "keep everything" knob.
pub fn spawn(
    dir: PathBuf,
    prefix: String,
    retention_days: u32,
    cancel: CancellationToken,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(SWEEP_INTERVAL);
        // First tick fires immediately. Skip it so we don't race boot.
        ticker.tick().await;
        loop {
            tokio::select! {
                _ = cancel.cancelled() => {
                    tracing::debug!("log retention task: cancelled");
                    break;
                }
                _ = ticker.tick() => {
                    let removed = sweep_once(&dir, &prefix, retention_days);
                    if removed > 0 {
                        tracing::info!(
                            removed,
                            dir = %dir.display(),
                            retention_days,
                            "log retention sweep removed aged files",
                        );
                    }
                }
            }
        }
    })
}

/// One sweep pass. Returns the number of files deleted. Exposed for
/// testing. `retention_days = 0` short-circuits to `0`.
pub fn sweep_once(dir: &Path, prefix: &str, retention_days: u32) -> usize {
    if retention_days == 0 {
        return 0;
    }
    let Ok(read) = std::fs::read_dir(dir) else {
        // Missing directory is fine — the sink may not have written yet.
        return 0;
    };
    let cutoff = Duration::from_secs(u64::from(retention_days) * 86_400);
    let now = std::time::SystemTime::now();
    let matcher = rotated_file_regex(prefix);

    let mut removed = 0usize;
    for entry in read.flatten() {
        let Ok(ft) = entry.file_type() else { continue };
        if !ft.is_file() {
            continue;
        }
        let name_os = entry.file_name();
        let Some(name) = name_os.to_str() else {
            continue;
        };
        if !matcher.is_match(name) {
            continue;
        }
        // Protect the currently-active file (bare prefix, no date suffix)
        // so `Rotation::NEVER` deployments aren't unlinked from under the
        // appender on the first sweep.
        if name == prefix {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        let Ok(mtime) = meta.modified() else { continue };
        let Ok(age) = now.duration_since(mtime) else {
            continue;
        };
        if age < cutoff {
            continue;
        }
        match std::fs::remove_file(entry.path()) {
            Ok(()) => {
                removed += 1;
                LOG_FILES_REMOVED.with_label_values(&["age"]).inc();
            }
            Err(err) => {
                tracing::warn!(
                    file = %entry.path().display(),
                    error = %err,
                    "log retention: remove_file failed",
                );
            }
        }
    }
    removed
}

/// Build the regex that matches any rotated log file for `prefix`,
/// covering daily / hourly / minutely suffixes. The bare `<prefix>`
/// (never-rotation) also matches so the caller can explicitly skip it.
fn rotated_file_regex(prefix: &str) -> Regex {
    // Suffix is YYYY-MM-DD optionally followed by -HH and -mm.
    let pattern = format!(
        r"^{pfx}(\.\d{{4}}-\d{{2}}-\d{{2}}(-\d{{2}}){{0,2}})?$",
        pfx = regex::escape(prefix)
    );
    Regex::new(&pattern).expect("static regex")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, File};
    use std::time::SystemTime;

    fn touch(path: &Path, age_days: u64) {
        let f = File::create(path).unwrap();
        if age_days > 0 {
            let mtime = SystemTime::now() - Duration::from_secs(age_days * 86_400 + 60);
            f.set_modified(mtime).unwrap();
        }
    }

    #[test]
    fn regex_matches_every_rotation_suffix() {
        let re = rotated_file_regex("gateway.log");
        assert!(re.is_match("gateway.log"));
        assert!(re.is_match("gateway.log.2026-04-23"));
        assert!(re.is_match("gateway.log.2026-04-23-07"));
        assert!(re.is_match("gateway.log.2026-04-23-07-45"));
        assert!(!re.is_match("gateway.log.bak"));
        assert!(!re.is_match("other.log.2026-04-23"));
    }

    #[test]
    fn sweep_skips_young_files_and_removes_aged() {
        let dir = tempfile::tempdir().unwrap();
        let young = dir.path().join("gateway.log.2026-04-22");
        let old = dir.path().join("gateway.log.2026-01-01");
        let unrelated = dir.path().join("notes.txt");
        touch(&young, 1);
        touch(&old, 30);
        touch(&unrelated, 30);

        let removed = sweep_once(dir.path(), "gateway.log", 7);
        assert_eq!(removed, 1);
        assert!(young.exists(), "young file should survive");
        assert!(!old.exists(), "old file should be removed");
        assert!(unrelated.exists(), "unrelated file should be untouched");
    }

    #[test]
    fn sweep_preserves_active_never_rotation_file() {
        let dir = tempfile::tempdir().unwrap();
        let active = dir.path().join("gateway.log");
        touch(&active, 99);
        let removed = sweep_once(dir.path(), "gateway.log", 7);
        assert_eq!(removed, 0);
        assert!(active.exists(), "active file must not be deleted");
    }

    #[test]
    fn sweep_with_zero_retention_is_noop() {
        let dir = tempfile::tempdir().unwrap();
        let old = dir.path().join("gateway.log.2024-01-01");
        touch(&old, 400);
        let removed = sweep_once(dir.path(), "gateway.log", 0);
        assert_eq!(removed, 0);
        assert!(old.exists());
    }

    #[test]
    fn sweep_missing_dir_is_noop() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("does-not-exist");
        // No panic, zero removed.
        assert_eq!(sweep_once(&missing, "gateway.log", 7), 0);
        // Clean up no-op.
        let _ = fs::remove_dir_all(dir.path());
    }
}
