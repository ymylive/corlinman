//! Metric snapshot + delta computation against `evolution_signals`.
//!
//! The W1-B contract: at apply time the Applier writes a [`MetricSnapshot`]
//! JSON into `evolution_history.metrics_baseline`. At monitor time we take
//! a fresh [`MetricSnapshot`] over the same window length and feed both
//! into [`compute_delta`] → [`breaches_threshold`].
//!
//! Why `evolution_signals` and not Prometheus: the monitor lives inside
//! the gateway process tree and we already store severity-typed event
//! rows there. No scrape, no second time-series store, and the Python
//! engine writes via the same table — single source of truth.
//!
//! `BTreeMap` (not `HashMap`) keeps the JSON serialization stable so two
//! snapshots taken back-to-back diff cleanly.

use std::collections::BTreeMap;

use corlinman_core::config::AutoRollbackThresholds;
use corlinman_evolution::EvolutionKind;
use serde::{Deserialize, Serialize};
use sqlx::SqlitePool;

/// Per-event-kind signal counts captured over a sliding window. The
/// applier writes this as JSON into `evolution_history.metrics_baseline`
/// at apply time; the monitor takes a fresh one and computes a delta.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricSnapshot {
    pub target: String,
    pub captured_at_ms: i64,
    pub window_secs: u32,
    /// `event_kind` → count over the window. Empty when the slice of
    /// watched kinds was empty (kind not yet wired for AutoRollback).
    pub counts: BTreeMap<String, u64>,
}

/// Computed delta between two snapshots. Used by the monitor's threshold
/// check; carried into `auto_rollback_reason` as a human-readable summary
/// when a rollback fires.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricDelta {
    pub target: String,
    pub baseline_total: u64,
    pub current_total: u64,
    pub abs_delta: i64,
    /// `(current_total - baseline_total) / max(baseline_total, 1) * 100`.
    /// Denominator floored at 1 keeps quiet targets NaN-free.
    pub rel_pct: f64,
    pub per_event_kind: BTreeMap<String, KindDelta>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct KindDelta {
    pub baseline: u64,
    pub current: u64,
    pub abs_delta: i64,
    pub rel_pct: f64,
}

/// Which `evolution_signals.event_kind` values count as a regression
/// signal for a given EvolutionKind. Targeted to start: memory_op only.
/// New kinds extend the match arm as their handlers land.
pub fn watched_event_kinds(kind: EvolutionKind) -> &'static [&'static str] {
    match kind {
        EvolutionKind::MemoryOp => &["tool.call.failed", "search.recall.dropped"],
        // Other kinds: empty slice means "monitor sees no signals" so we
        // never auto-rollback a kind we don't yet have a signal contract
        // for. Safer than guessing.
        _ => &[],
    }
}

/// Count signals over a sliding window per event_kind, filtered to
/// `warn`/`error` severity (info is noise for regression purposes).
///
/// Empty `event_kinds` → empty `counts` map; the rest of the snapshot is
/// still populated so the applier can persist a stable baseline shape
/// and Step 4 can detect "no whitelist for this kind" without a special
/// case.
pub async fn capture_snapshot(
    pool: &SqlitePool,
    target: &str,
    event_kinds: &[&str],
    window_secs: u32,
    now_ms: i64,
) -> Result<MetricSnapshot, sqlx::Error> {
    let since_ms = now_ms - (window_secs as i64) * 1000;
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();

    // One parameterised query per event_kind. The set is small (memory_op
    // = 2 today, max ~10 per kind) so per-kind round-trips are cheaper
    // than building a dynamic IN clause.
    for kind in event_kinds {
        let row: (i64,) = sqlx::query_as(
            r#"SELECT COUNT(*) FROM evolution_signals
               WHERE target = ?
                 AND event_kind = ?
                 AND observed_at >= ?
                 AND severity IN ('warn', 'error')"#,
        )
        .bind(target)
        .bind(*kind)
        .bind(since_ms)
        .fetch_one(pool)
        .await?;
        counts.insert((*kind).to_string(), row.0 as u64);
    }

    Ok(MetricSnapshot {
        target: target.to_string(),
        captured_at_ms: now_ms,
        window_secs,
        counts,
    })
}

/// Pure diff between two snapshots. The union of event_kinds across both
/// inputs guarantees a stable shape even when the whitelist changes
/// between baseline-capture and current-capture (e.g. config edit
/// mid-grace-window).
pub fn compute_delta(baseline: &MetricSnapshot, current: &MetricSnapshot) -> MetricDelta {
    let mut all_kinds: std::collections::BTreeSet<&str> = std::collections::BTreeSet::new();
    for k in baseline.counts.keys() {
        all_kinds.insert(k.as_str());
    }
    for k in current.counts.keys() {
        all_kinds.insert(k.as_str());
    }

    let mut per_event_kind = BTreeMap::new();
    let mut baseline_total: u64 = 0;
    let mut current_total: u64 = 0;
    for kind in all_kinds {
        let b = baseline.counts.get(kind).copied().unwrap_or(0);
        let c = current.counts.get(kind).copied().unwrap_or(0);
        baseline_total += b;
        current_total += c;
        let abs_delta = c as i64 - b as i64;
        let denom = b.max(1) as f64;
        let rel_pct = (abs_delta as f64 / denom) * 100.0;
        per_event_kind.insert(
            kind.to_string(),
            KindDelta {
                baseline: b,
                current: c,
                abs_delta,
                rel_pct,
            },
        );
    }

    let abs_delta = current_total as i64 - baseline_total as i64;
    let denom = baseline_total.max(1) as f64;
    let rel_pct = (abs_delta as f64 / denom) * 100.0;

    MetricDelta {
        // Both snapshots target the same proposal; pick baseline's.
        target: baseline.target.clone(),
        baseline_total,
        current_total,
        abs_delta,
        rel_pct,
        per_event_kind,
    }
}

/// Decide whether a delta breaches the configured rollback threshold.
///
/// Returns `Some(reason)` only when both:
/// - `baseline_total >= min_baseline_signals` (quiet-target guard — a
///   target that emitted near-zero pre-apply doesn't deserve a rollback
///   on the first post-apply spike), and
/// - `rel_pct >= default_err_rate_delta_pct`.
///
/// `default_p95_latency_delta_pct` is intentionally not consulted here:
/// W1-B's memory_op path doesn't emit latency-bucketed signals, so
/// folding it into the breach test would be lying about what we're
/// measuring. Future kinds that emit latency signals get their own
/// branch in this function.
pub fn breaches_threshold(
    delta: &MetricDelta,
    thresholds: &AutoRollbackThresholds,
) -> Option<String> {
    if delta.baseline_total < thresholds.min_baseline_signals as u64 {
        return None;
    }
    if delta.rel_pct < thresholds.default_err_rate_delta_pct {
        return None;
    }
    let sign = if delta.abs_delta >= 0 { "+" } else { "" };
    Some(format!(
        "err_signal_count: {} -> {} ({sign}{:.0}%) breaches threshold +{:.0}%",
        delta.baseline_total,
        delta.current_total,
        delta.rel_pct,
        thresholds.default_err_rate_delta_pct,
    ))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_evolution::EvolutionStore;
    use sqlx::SqlitePool;
    use tempfile::TempDir;

    /// Open a fresh evolution.sqlite in a tempdir and hand back its pool.
    /// Each test gets its own DB so signals don't leak across tests.
    async fn fresh_pool() -> (TempDir, SqlitePool) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();
        let pool = store.pool().clone();
        (tmp, pool)
    }

    /// Insert one signal row directly via SQL — bypasses the repo so
    /// tests can construct edge cases the typed API would reject.
    async fn seed_signal(
        pool: &SqlitePool,
        event_kind: &str,
        target: &str,
        severity: &str,
        observed_at: i64,
    ) {
        sqlx::query(
            r#"INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json, observed_at)
               VALUES (?, ?, ?, '{}', ?)"#,
        )
        .bind(event_kind)
        .bind(target)
        .bind(severity)
        .bind(observed_at)
        .execute(pool)
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn capture_snapshot_empty_db() {
        let (_tmp, pool) = fresh_pool().await;
        let snap = capture_snapshot(
            &pool,
            "delete_chunk:1",
            &["tool.call.failed", "search.recall.dropped"],
            1_800,
            10_000_000,
        )
        .await
        .unwrap();
        assert_eq!(snap.target, "delete_chunk:1");
        assert_eq!(snap.window_secs, 1_800);
        assert_eq!(snap.captured_at_ms, 10_000_000);
        assert_eq!(snap.counts.get("tool.call.failed"), Some(&0));
        assert_eq!(snap.counts.get("search.recall.dropped"), Some(&0));
    }

    #[tokio::test]
    async fn capture_snapshot_filters_by_target_and_window() {
        let (_tmp, pool) = fresh_pool().await;
        let now = 10_000_000_i64;
        // In-window, matching target — should count.
        seed_signal(&pool, "tool.call.failed", "delete_chunk:1", "error", now - 60_000).await;
        seed_signal(&pool, "tool.call.failed", "delete_chunk:1", "warn", now - 1_000).await;
        // In-window, *different* target — must be excluded.
        seed_signal(&pool, "tool.call.failed", "delete_chunk:99", "error", now - 1_000).await;
        // Matching target, but observed_at older than the window.
        seed_signal(
            &pool,
            "tool.call.failed",
            "delete_chunk:1",
            "error",
            now - 10_000_000,
        )
        .await;

        let snap = capture_snapshot(
            &pool,
            "delete_chunk:1",
            &["tool.call.failed"],
            1_800,
            now,
        )
        .await
        .unwrap();
        assert_eq!(snap.counts.get("tool.call.failed"), Some(&2));
    }

    #[tokio::test]
    async fn capture_snapshot_filters_severity() {
        let (_tmp, pool) = fresh_pool().await;
        let now = 10_000_000_i64;
        // info-severity is noise — must not show up in regression counts.
        seed_signal(&pool, "tool.call.failed", "t", "info", now - 1_000).await;
        seed_signal(&pool, "tool.call.failed", "t", "warn", now - 1_000).await;
        seed_signal(&pool, "tool.call.failed", "t", "error", now - 1_000).await;

        let snap = capture_snapshot(&pool, "t", &["tool.call.failed"], 1_800, now)
            .await
            .unwrap();
        assert_eq!(snap.counts.get("tool.call.failed"), Some(&2));
    }

    #[tokio::test]
    async fn capture_snapshot_empty_event_kinds_returns_empty_counts() {
        let (_tmp, pool) = fresh_pool().await;
        // Even when signals exist, an empty whitelist → empty counts —
        // documents the "no whitelist for this kind yet" path the
        // applier relies on.
        seed_signal(&pool, "tool.call.failed", "t", "error", 1_000).await;

        let snap = capture_snapshot(&pool, "t", &[], 1_800, 10_000_000)
            .await
            .unwrap();
        assert!(snap.counts.is_empty());
        assert_eq!(snap.target, "t");
        assert_eq!(snap.window_secs, 1_800);
    }

    fn snap(target: &str, counts: &[(&str, u64)]) -> MetricSnapshot {
        MetricSnapshot {
            target: target.into(),
            captured_at_ms: 0,
            window_secs: 1_800,
            counts: counts
                .iter()
                .map(|(k, v)| (k.to_string(), *v))
                .collect(),
        }
    }

    #[test]
    fn compute_delta_zero_baseline() {
        let baseline = snap("t", &[("tool.call.failed", 0)]);
        let current = snap("t", &[("tool.call.failed", 5)]);
        let d = compute_delta(&baseline, &current);
        assert_eq!(d.baseline_total, 0);
        assert_eq!(d.current_total, 5);
        // denom-floor-at-1 → no NaN/Inf even when baseline is zero.
        assert!(d.rel_pct.is_finite());
        assert_eq!(d.rel_pct, 500.0);
    }

    #[test]
    fn compute_delta_proportional() {
        let baseline = snap("t", &[("tool.call.failed", 4)]);
        let current = snap("t", &[("tool.call.failed", 6)]);
        let d = compute_delta(&baseline, &current);
        assert_eq!(d.baseline_total, 4);
        assert_eq!(d.current_total, 6);
        assert_eq!(d.abs_delta, 2);
        assert!((d.rel_pct - 50.0).abs() < 1e-9);
    }

    fn thresholds(min: u32, pct: f64) -> AutoRollbackThresholds {
        AutoRollbackThresholds {
            default_err_rate_delta_pct: pct,
            default_p95_latency_delta_pct: 25.0,
            signal_window_secs: 1_800,
            min_baseline_signals: min,
        }
    }

    #[test]
    fn breaches_threshold_quiet_target_no_alarm() {
        // baseline 0, current 100, but min_baseline_signals=5 → quiet
        // target guard kicks in, no rollback even though rel_pct is huge.
        let baseline = snap("t", &[("tool.call.failed", 0)]);
        let current = snap("t", &[("tool.call.failed", 100)]);
        let d = compute_delta(&baseline, &current);
        let t = thresholds(5, 50.0);
        assert!(breaches_threshold(&d, &t).is_none());
    }

    #[test]
    fn breaches_threshold_loud_target_above_pct() {
        let baseline = snap("t", &[("tool.call.failed", 10)]);
        let current = snap("t", &[("tool.call.failed", 20)]);
        let d = compute_delta(&baseline, &current);
        let t = thresholds(5, 50.0);
        let reason = breaches_threshold(&d, &t).expect("expected breach");
        assert!(reason.contains("10"));
        assert!(reason.contains("20"));
        assert!(reason.contains("breaches threshold"));
    }

    #[test]
    fn watched_event_kinds_memory_op_present() {
        let kinds = watched_event_kinds(EvolutionKind::MemoryOp);
        assert!(kinds.contains(&"tool.call.failed"));
        assert!(kinds.contains(&"search.recall.dropped"));
    }

    #[test]
    fn watched_event_kinds_unknown_returns_empty() {
        for k in [
            EvolutionKind::TagRebalance,
            EvolutionKind::RetryTuning,
            EvolutionKind::AgentCard,
            EvolutionKind::SkillUpdate,
            EvolutionKind::PromptTemplate,
            EvolutionKind::ToolPolicy,
            EvolutionKind::NewSkill,
        ] {
            assert!(watched_event_kinds(k).is_empty(), "{k:?} should be empty");
        }
    }
}
