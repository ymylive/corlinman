//! `ConfigWatcher` — SIGHUP + filesystem hot-reload for `corlinman.toml`.
//!
//! Until B5-BE3, every TOML edit required a full gateway restart. This module
//! closes that gap:
//!
//! * A `notify::recommended_watcher` is installed on the config file's
//!   *parent* directory (editors commonly atomic-rename the file, which means
//!   the inode watchers see zero modify events on the old path). Create /
//!   modify / remove events whose path matches the config file trigger a
//!   debounced reload.
//! * On Unix, a `SIGHUP` handler calls `trigger_reload` directly — matches the
//!   classic daemon idiom so operators can `killall -HUP corlinman-gateway`.
//! * A debouncer coalesces the `notify` burst (macOS FSEvents routinely fires
//!   3-5 events per save; Linux `inotify` splits one atomic rename across
//!   `CREATE` + `MOVED_FROM`/`MOVED_TO`) into a single parse attempt.
//! * On every successful reload the new [`Config`] is diffed against the
//!   current snapshot at the section level (top-level struct fields). Each
//!   differing section emits a [`HookEvent::ConfigChanged`] before the
//!   `ArcSwap::store` publishes the new snapshot, so subscribers observing
//!   hooks don't see an intermediate state.
//! * Restart-required sections (`server`, `wstool`, `nodebridge`) still swap
//!   — the in-memory view is the source of truth — but additionally emit a
//!   `<section>.restart_required` event so the admin UI can surface a
//!   "process restart needed" warning.
//!
//! Failure model
//! -------------
//! * Parse failure → `ReloadReport.errors` populated; snapshot is **not**
//!   swapped and no hook events fire. Log-level is `warn`.
//! * Validation failure → same as parse failure: no swap, no hooks.
//! * Idempotent reload (file rewritten with same content) → empty report, no
//!   hooks — keeps the bus quiet for editors that rewrite-in-place on save.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use corlinman_core::config::{Config, IssueLevel};
use corlinman_hooks::{HookBus, HookEvent};
use notify::{recommended_watcher, Event, EventKind, RecursiveMode, Watcher};
use serde_json::Value;
use tokio::sync::mpsc::{self, UnboundedReceiver, UnboundedSender};
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

/// Debounce window for filesystem events. Tuned at 300ms — long enough to
/// coalesce a typical editor save (vim `:w`, VS Code atomic-rename, etc.) but
/// short enough to feel instant to an operator running `vim config.toml`.
pub const DEFAULT_DEBOUNCE: Duration = Duration::from_millis(300);

/// Top-level sections which cannot be applied without a process restart.
/// We still swap (the snapshot is the source of truth) but emit an extra
/// `<section>.restart_required` event so operators get a loud warning.
const RESTART_REQUIRED_SECTIONS: &[&str] = &["server", "wstool", "nodebridge", "mcp"];

/// Watcher over the gateway's live `corlinman.toml`. Clone-cheap (everything
/// is behind `Arc`s) so subsystems that want a cheap read-only handle to the
/// live config can call [`Self::current`] without paying a boot cost.
pub struct ConfigWatcher {
    path: PathBuf,
    bus: Arc<HookBus>,
    current: Arc<ArcSwap<Config>>,
    /// Serialises concurrent reloads. The watcher task and the admin
    /// `/admin/config/reload` endpoint can both race to reload; holding a
    /// mutex keeps the diff + emit + swap atomic from a subscriber POV.
    reload_lock: Arc<Mutex<()>>,
}

/// Result of a single reload attempt. Returned to the admin endpoint and
/// logged by the SIGHUP / fs watcher paths.
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct ReloadReport {
    /// Dotted section names that differed between the old and new snapshot.
    /// Entries flagged as restart-required will additionally appear as
    /// `<section>.restart_required`.
    pub changed_sections: Vec<String>,
    /// Non-fatal diagnostics (parse errors, validation failures). Non-empty
    /// implies the snapshot was *not* swapped.
    pub errors: Vec<String>,
}

impl ReloadReport {
    pub fn is_noop(&self) -> bool {
        self.changed_sections.is_empty() && self.errors.is_empty()
    }
}

impl ConfigWatcher {
    /// Build a new watcher bound to `path`. `initial` is published to the
    /// `ArcSwap` immediately so `current()` is safe to call before `run`
    /// starts the background task.
    pub fn new(path: PathBuf, initial: Config, bus: Arc<HookBus>) -> Self {
        Self {
            path,
            bus,
            current: Arc::new(ArcSwap::from_pointee(initial)),
            reload_lock: Arc::new(Mutex::new(())),
        }
    }

    /// Cheap snapshot of the current config. Readers should prefer this over
    /// caching their own `Arc<Config>` so hot-reload actually reaches them.
    pub fn current(&self) -> Arc<Config> {
        self.current.load_full()
    }

    /// Underlying `ArcSwap` handle — useful for subsystems (admin routes,
    /// `AdminAuthState`, ...) that already expect an `Arc<ArcSwap<Config>>`
    /// and want to share it with live-reload consumers.
    pub fn arc_swap(&self) -> Arc<ArcSwap<Config>> {
        self.current.clone()
    }

    /// Spawn the watcher. Runs until `cancel` fires. The `notify` watcher is
    /// joined on cancellation so fs handles don't leak. Takes `Arc<Self>` so
    /// callers (e.g. `main.rs`) can share the same watcher handle with the
    /// admin router while the background task owns an additional Arc.
    pub async fn run(self: Arc<Self>, cancel: CancellationToken) -> anyhow::Result<()> {
        // The fs watcher channel + install happens first so the run loop can
        // keep the handle alive (dropping `_watcher` stops delivery).
        let (tx, rx) = mpsc::unbounded_channel::<WatchEvent>();
        let _watcher = match install_watcher(&self.path, tx.clone()) {
            Ok(w) => Some(w),
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    path = %self.path.display(),
                    "config watcher: notify install failed; SIGHUP-only reload",
                );
                None
            }
        };

        // SIGHUP handler, Unix only. Windows gets the fs watcher + the admin
        // endpoint for manual reload.
        let sighup_task = spawn_sighup_handler(self.clone(), cancel.clone());

        run_loop(self.clone(), rx, DEFAULT_DEBOUNCE, cancel.clone()).await;

        // Make sure the SIGHUP task isn't leaked if the main loop exits
        // before cancellation lands.
        if let Some(h) = sighup_task {
            let _ = h.await;
        }
        Ok(())
    }

    /// Publicly-callable reload trigger — used by `/admin/config/reload` and
    /// the SIGHUP handler. Returns the report so callers can surface it.
    pub async fn trigger_reload(&self) -> anyhow::Result<ReloadReport> {
        // Serialise: the fs watcher, admin endpoint and SIGHUP handler can
        // all race. Holding the lock means the diff + hook emits + ArcSwap
        // publish appear atomic to subscribers.
        let _guard = self.reload_lock.lock().await;

        let mut report = ReloadReport::default();

        // Stage 1: parse. I/O + decode failures both land in `errors` so the
        // operator sees a single unified reason for the skipped reload.
        let new_config = match Config::load_from_path(&self.path) {
            Ok(c) => c,
            Err(err) => {
                let msg = format!("parse failed: {err}");
                tracing::warn!(path = %self.path.display(), error = %err, "config reload: parse failed");
                report.errors.push(msg);
                return Ok(report);
            }
        };

        // Stage 2: validate. We only gate on `Error`-level issues so a
        // brand-new config with a provider warning still hot-loads.
        let issues = new_config.validate_report();
        let mut had_error = false;
        for issue in &issues {
            if issue.level == IssueLevel::Error {
                had_error = true;
                report
                    .errors
                    .push(format!("{}: {}: {}", issue.path, issue.code, issue.message));
            }
        }
        if had_error {
            tracing::warn!(
                path = %self.path.display(),
                errors = ?report.errors,
                "config reload: validation failed",
            );
            return Ok(report);
        }

        // Stage 3: diff against current. Section = one top-level struct field.
        let old = self.current.load_full();
        let changed = diff_sections(&old, &new_config);

        if changed.is_empty() {
            tracing::debug!(path = %self.path.display(), "config reload: no-op (identical content)");
            return Ok(report);
        }

        // Stage 4: swap in-memory snapshot first so subscribers that call
        // `current()` inside their handler observe the new state — the
        // `old`/`new` fields on the event carry both views for consumers
        // that want to reason about the delta directly.
        self.current.store(Arc::new(new_config.clone()));

        // Stage 5: emit ConfigChanged per section. Emitted in `diff_sections`
        // order (lexicographic over the SECTIONS list).
        for section in &changed {
            let old_val = section_value(&old, section);
            let new_val = section_value(&new_config, section);
            let event = HookEvent::ConfigChanged {
                section: section.clone(),
                old: old_val,
                new: new_val,
            };
            if let Err(err) = self.bus.emit(event).await {
                tracing::warn!(error = %err, section, "hook bus emit ConfigChanged failed");
            }
        }

        // Stage 6: restart-required flags. Same ordering — emit after the
        // base-section event so subscribers see a predictable sequence.
        for section in &changed {
            if RESTART_REQUIRED_SECTIONS.contains(&section.as_str()) {
                let flag = format!("{section}.restart_required");
                // `old`/`new` carry the field map so a UI can pinpoint which
                // leaf moved. `new` is what's now live.
                let new_snapshot = self.current.load_full();
                let event = HookEvent::ConfigChanged {
                    section: flag.clone(),
                    old: section_value(&old, section),
                    new: section_value(&new_snapshot, section),
                };
                if let Err(err) = self.bus.emit(event).await {
                    tracing::warn!(error = %err, section = %flag, "hook bus emit restart_required failed");
                }
                tracing::warn!(
                    section = %section,
                    "config reload: section changed but requires process restart to fully take effect",
                );
            }
        }

        tracing::info!(
            path = %self.path.display(),
            changed = ?changed,
            "config reload: applied",
        );
        report.changed_sections = changed;
        Ok(report)
    }
}

// ---------------------------------------------------------------------------
// Watcher plumbing
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct WatchEvent;

fn install_watcher(
    path: &Path,
    tx: UnboundedSender<WatchEvent>,
) -> Result<notify::RecommendedWatcher, notify::Error> {
    // Watch the parent directory — atomic-rename saves (vim, VS Code) swap
    // the inode out from under us, so a watch on the file itself misses the
    // very events we care about. Match on the final path component in the
    // closure so unrelated files in the same dir don't trigger reloads.
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let target = path.to_path_buf();

    let mut watcher = recommended_watcher(move |res: notify::Result<Event>| match res {
        Ok(event) => {
            if !matters(&event) {
                return;
            }
            if event.paths.iter().any(|p| same_file(p, &target)) {
                let _ = tx.send(WatchEvent);
            }
        }
        Err(err) => {
            tracing::warn!(error = %err, "config watcher: notify event error");
        }
    })?;

    // Non-recursive: we only care about the single config file.
    if parent.exists() {
        watcher.watch(parent, RecursiveMode::NonRecursive)?;
    } else {
        tracing::warn!(
            path = %parent.display(),
            "config watcher: parent dir missing; watcher not installed",
        );
    }
    Ok(watcher)
}

fn matters(event: &Event) -> bool {
    matches!(
        event.kind,
        EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_)
    )
}

/// Compare two paths canonically — notify hands us the absolute path (on
/// most platforms), but our config path may be relative. Fall back to a
/// filename-only comparison when canonicalise fails (file was just deleted).
fn same_file(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => a.file_name() == b.file_name() && a.file_name().is_some(),
    }
}

fn spawn_sighup_handler(
    handle: Arc<ConfigWatcher>,
    cancel: CancellationToken,
) -> Option<tokio::task::JoinHandle<()>> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut hup = match signal(SignalKind::hangup()) {
            Ok(s) => s,
            Err(err) => {
                tracing::warn!(error = %err, "config watcher: failed to register SIGHUP");
                return None;
            }
        };
        let task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => {
                        tracing::debug!("config watcher: SIGHUP task cancelled");
                        return;
                    }
                    maybe = hup.recv() => {
                        if maybe.is_none() {
                            return;
                        }
                        tracing::info!("SIGHUP received; reloading config");
                        match handle.trigger_reload().await {
                            Ok(report) if report.is_noop() => {
                                tracing::debug!("SIGHUP reload: no-op");
                            }
                            Ok(report) => {
                                tracing::info!(
                                    changed = ?report.changed_sections,
                                    errors = ?report.errors,
                                    "SIGHUP reload: done",
                                );
                            }
                            Err(err) => {
                                tracing::warn!(error = %err, "SIGHUP reload: unexpected failure");
                            }
                        }
                    }
                }
            }
        });
        Some(task)
    }
    #[cfg(not(unix))]
    {
        let _ = (handle, cancel);
        tracing::info!("config watcher: SIGHUP unsupported on this platform");
        None
    }
}

async fn run_loop(
    handle: Arc<ConfigWatcher>,
    mut rx: UnboundedReceiver<WatchEvent>,
    debounce: Duration,
    cancel: CancellationToken,
) {
    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::debug!("config watcher: run loop cancelled");
                return;
            }
            maybe = rx.recv() => {
                if maybe.is_none() {
                    tracing::debug!("config watcher: event channel closed");
                    return;
                }
                // Drain everything that arrived within the debounce window
                // so a burst of notify events only produces one reload.
                let deadline = tokio::time::Instant::now() + debounce;
                loop {
                    tokio::select! {
                        _ = cancel.cancelled() => return,
                        _ = tokio::time::sleep_until(deadline) => break,
                        more = rx.recv() => {
                            if more.is_none() {
                                break;
                            }
                        }
                    }
                }
                match handle.trigger_reload().await {
                    Ok(report) if report.is_noop() => {
                        tracing::debug!("fs reload: no-op");
                    }
                    Ok(report) => tracing::info!(
                        changed = ?report.changed_sections,
                        errors = ?report.errors,
                        "fs reload: done",
                    ),
                    Err(err) => tracing::warn!(error = %err, "fs reload: unexpected failure"),
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Section diff helpers
// ---------------------------------------------------------------------------

/// Section names we diff on. Matches every top-level field of `Config` —
/// ordering is irrelevant, `diff_sections` sorts them.
const SECTIONS: &[&str] = &[
    "server",
    "admin",
    "providers",
    "models",
    "embedding",
    "channels",
    "rag",
    "approvals",
    "scheduler",
    "logging",
    "hooks",
    "skills",
    "variables",
    "agents",
    "tools",
    "telegram",
    "vector",
    "wstool",
    "canvas",
    "nodebridge",
    "meta",
];

/// Return the ordered list of section names whose JSON representation
/// differs between `old` and `new`.
pub(crate) fn diff_sections(old: &Config, new: &Config) -> Vec<String> {
    let old_v = serde_json::to_value(old).unwrap_or(Value::Null);
    let new_v = serde_json::to_value(new).unwrap_or(Value::Null);
    let mut out = BTreeSet::new();
    for section in SECTIONS {
        let o = old_v.get(section).cloned().unwrap_or(Value::Null);
        let n = new_v.get(section).cloned().unwrap_or(Value::Null);
        if o != n {
            out.insert((*section).to_string());
        }
    }
    out.into_iter().collect()
}

/// Pull a single section's JSON representation out of `cfg`, returning
/// `Null` on serialisation failure (should never happen in practice — the
/// config serialises to JSON every time the admin `GET /admin/config`
/// handler runs).
fn section_value(cfg: &Config, section: &str) -> Value {
    match serde_json::to_value(cfg) {
        Ok(Value::Object(mut map)) => map.remove(section).unwrap_or(Value::Null),
        _ => Value::Null,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{ProviderEntry, SecretRef};
    use corlinman_hooks::HookPriority;

    fn base_cfg() -> Config {
        let mut cfg = Config::default();
        // Default seeds a disabled `openai` entry; replace it with anthropic
        // so this fixture's `enabled_names()` expectation stays singular.
        cfg.providers.remove("openai");
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                enabled: true,
                ..Default::default()
            },
        );
        cfg
    }

    #[test]
    fn diff_detects_models_default_change() {
        let mut a = base_cfg();
        let mut b = base_cfg();
        a.models.default = "claude-sonnet-4-5".into();
        b.models.default = "claude-opus-4-7".into();
        assert_eq!(diff_sections(&a, &b), vec!["models".to_string()]);
    }

    #[test]
    fn diff_empty_on_identical_configs() {
        let a = base_cfg();
        let b = base_cfg();
        assert!(diff_sections(&a, &b).is_empty());
    }

    #[test]
    fn diff_flags_multiple_sections() {
        let mut a = base_cfg();
        let mut b = base_cfg();
        b.models.default = "claude-opus-4-7".into();
        b.server.port = 7001;
        a.logging.level = "info".into();
        b.logging.level = "debug".into();
        let sections = diff_sections(&a, &b);
        assert!(sections.contains(&"models".to_string()));
        assert!(sections.contains(&"server".to_string()));
        assert!(sections.contains(&"logging".to_string()));
    }

    #[tokio::test]
    async fn parse_failure_reports_error_and_does_not_swap() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("config.toml");
        // Seed a valid file so `new()` has something to echo.
        std::fs::write(&path, "").unwrap();
        let bus = Arc::new(HookBus::new(16));
        let watcher = ConfigWatcher::new(path.clone(), base_cfg(), bus);

        // Garbage TOML.
        std::fs::write(&path, "::::not-toml::::").unwrap();
        let report = watcher.trigger_reload().await.unwrap();
        assert!(!report.errors.is_empty(), "expected parse error");
        assert!(report.changed_sections.is_empty());
        // Snapshot unchanged.
        assert_eq!(
            watcher.current().providers.enabled_names(),
            vec!["anthropic".to_string()]
        );
    }

    #[tokio::test]
    async fn identical_reload_is_noop() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("config.toml");
        let cfg = base_cfg();
        std::fs::write(&path, toml::to_string_pretty(&cfg).unwrap()).unwrap();
        let bus = Arc::new(HookBus::new(16));
        let watcher = ConfigWatcher::new(path.clone(), cfg, bus.clone());

        let mut sub = bus.subscribe(HookPriority::Normal);
        let report = watcher.trigger_reload().await.unwrap();
        assert!(report.is_noop(), "expected no-op, got {report:?}");
        // No hook event emitted — confirm the channel is empty.
        assert!(tokio::time::timeout(Duration::from_millis(50), sub.recv())
            .await
            .is_err());
    }
}
