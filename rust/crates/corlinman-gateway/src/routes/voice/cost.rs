//! Cost-gating primitives for the `/voice` route.
//!
//! Iter 3 of D4. Three layers, in priority order:
//!
//! 1. **Feature flag** — already enforced upstream by the route
//!    handler in [`super::voice_handler`].
//! 2. **Per-tenant daily minutes budget** — a session-start check
//!    refuses `[budget_minutes_per_tenant_per_day]` overage with
//!    HTTP 429 / `budget_exhausted`. Mid-session, a 1-Hz ticker
//!    drives [`SessionMeter::poll`] which transitions through
//!    `Ok` → `Warn` (60 s before cap) → `Exhausted` (terminate).
//! 3. **Hard kill at session length cap** — a per-session timer
//!    independent of the daily budget. Defends against a stuck
//!    session no client has the courtesy to end.
//!
//! Iter 8 will swap [`InMemoryVoiceSpend`] for a SQLite-backed
//! `voice_spend` table; the [`VoiceSpend`] trait surface is the
//! seam for that swap. Keeping the pure logic separate from any I/O
//! makes the Phase 4 D4 cost gate testable without a real provider.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use corlinman_core::config::VoiceConfig;

/// A single day's spend bucket per tenant. We track seconds (not
/// minutes) so partial-minute drift doesn't accumulate; the budget is
/// expressed in minutes and converted at check time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DaySpend {
    /// Days since UNIX epoch (UTC). Used as the bucket key so the
    /// counter rolls over at midnight UTC without an explicit reset.
    pub day_epoch: u64,
    /// Seconds spent on voice in this day so far.
    pub seconds_used: u64,
    /// Number of voice sessions started today (success + failure).
    pub sessions_count: u64,
}

impl DaySpend {
    fn fresh(day_epoch: u64) -> Self {
        Self {
            day_epoch,
            seconds_used: 0,
            sessions_count: 0,
        }
    }
}

/// Trait surface for spend accounting. Iter 3 supplies the in-memory
/// [`InMemoryVoiceSpend`]; iter 8 swaps in a SQLite-backed impl that
/// survives gateway restarts.
pub trait VoiceSpend: Send + Sync {
    /// Snapshot the current day's spend for the given tenant.
    fn snapshot(&self, tenant: &str, day_epoch: u64) -> DaySpend;

    /// Record a started session (the budget check has already cleared
    /// it). Returns the snapshot post-increment so callers can log
    /// the new sessions_count without a second read.
    fn record_session_start(&self, tenant: &str, day_epoch: u64) -> DaySpend;

    /// Add `seconds` of usage to the day's total (called when the
    /// session ends or when the per-second ticker runs).
    fn add_seconds(&self, tenant: &str, day_epoch: u64, seconds: u64) -> DaySpend;
}

/// Process-local spend store. Single-tenant deployments and tests use
/// this; multi-tenant production swaps to the SQLite impl in iter 8.
#[derive(Debug, Default)]
pub struct InMemoryVoiceSpend {
    inner: Mutex<HashMap<(String, u64), DaySpend>>,
}

impl InMemoryVoiceSpend {
    pub fn new() -> Self {
        Self::default()
    }
}

impl VoiceSpend for InMemoryVoiceSpend {
    fn snapshot(&self, tenant: &str, day_epoch: u64) -> DaySpend {
        let key = (tenant.to_string(), day_epoch);
        let map = self.inner.lock().expect("voice spend mutex poisoned");
        map.get(&key).copied().unwrap_or_else(|| DaySpend::fresh(day_epoch))
    }

    fn record_session_start(&self, tenant: &str, day_epoch: u64) -> DaySpend {
        let key = (tenant.to_string(), day_epoch);
        let mut map = self.inner.lock().expect("voice spend mutex poisoned");
        let entry = map.entry(key).or_insert_with(|| DaySpend::fresh(day_epoch));
        entry.sessions_count = entry.sessions_count.saturating_add(1);
        *entry
    }

    fn add_seconds(&self, tenant: &str, day_epoch: u64, seconds: u64) -> DaySpend {
        let key = (tenant.to_string(), day_epoch);
        let mut map = self.inner.lock().expect("voice spend mutex poisoned");
        let entry = map.entry(key).or_insert_with(|| DaySpend::fresh(day_epoch));
        entry.seconds_used = entry.seconds_used.saturating_add(seconds);
        *entry
    }
}

// ---------------------------------------------------------------------------
// Budget check (session-start)
// ---------------------------------------------------------------------------

/// Outcome of a session-start budget check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetDecision {
    /// Session may proceed. Carries the seconds remaining so the
    /// caller can decide whether to also schedule a `budget_warning`
    /// near the cap.
    Allow { seconds_remaining: u64 },
    /// Session refused at start. Maps to HTTP 429 with the supplied
    /// `reset_at` UNIX timestamp (next UTC midnight).
    Deny { reason: BudgetDenyReason, reset_at: u64 },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetDenyReason {
    /// Tenant has used >= the daily cap.
    DayBudgetExhausted {
        used_seconds: u64,
        cap_seconds: u64,
    },
    /// `budget_minutes_per_tenant_per_day = 0` — operator explicitly
    /// disabled voice for this tenant by zeroing the cap.
    BudgetIsZero,
}

/// Pure budget check. Decoupled from the spend store so iter 8 can
/// reuse the same logic against a SQLite-snapshot.
pub fn evaluate_budget(
    cfg: &VoiceConfig,
    today: DaySpend,
    next_midnight_unix_secs: u64,
) -> BudgetDecision {
    let cap_minutes = cfg.budget_minutes_per_tenant_per_day as u64;
    if cap_minutes == 0 {
        return BudgetDecision::Deny {
            reason: BudgetDenyReason::BudgetIsZero,
            reset_at: next_midnight_unix_secs,
        };
    }
    let cap_seconds = cap_minutes.saturating_mul(60);
    if today.seconds_used >= cap_seconds {
        return BudgetDecision::Deny {
            reason: BudgetDenyReason::DayBudgetExhausted {
                used_seconds: today.seconds_used,
                cap_seconds,
            },
            reset_at: next_midnight_unix_secs,
        };
    }
    BudgetDecision::Allow {
        seconds_remaining: cap_seconds - today.seconds_used,
    }
}

// ---------------------------------------------------------------------------
// Mid-session ticker — per-session second counter
// ---------------------------------------------------------------------------

/// Outcome of a single tick of the mid-session meter. The route
/// handler's 1-Hz ticker drives [`SessionMeter::poll`] and acts on the
/// returned variant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MeterTick {
    /// Session healthy; keep going.
    Ok,
    /// Approaching the day budget. The session is still alive but the
    /// gateway should emit a `budget_warning` event so the client UI
    /// can warn the user. Fires once when crossing the threshold.
    BudgetWarn {
        /// Whole minutes remaining (rounded down). Sent verbatim in
        /// the `budget_warning.minutes_remaining` field.
        minutes_remaining: u32,
    },
    /// Either the day budget or the per-session length cap is hit.
    /// The handler must close the WebSocket with the supplied close
    /// code (4002 budget, 4003 max-session) and write the
    /// `voice_sessions.end_reason` row.
    Terminate {
        reason: TerminateReason,
        close_code: u16,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TerminateReason {
    /// Daily-budget cap reached mid-session.
    DayBudgetExhausted,
    /// Per-session length cap reached.
    MaxSessionSeconds,
}

/// Per-session meter. Holds the wall-clock start, the latest budget
/// snapshot at session-start, and the configured caps. The ticker
/// calls `poll(now)` every ~1 s; each call returns `MeterTick`.
///
/// `start_seconds_used` is the **day** counter at session start; it
/// lets us compute the day budget without re-querying the spend
/// store on every tick. The store is only updated via
/// [`VoiceSpend::add_seconds`] when the session ends (or, in iter 8,
/// when an in-flight checkpoint runs).
#[derive(Debug)]
pub struct SessionMeter {
    started_at: Instant,
    cap_seconds: u64,
    max_session_seconds: u64,
    start_seconds_used: u64,
    /// Has the warn event already fired? Latches so we never emit
    /// `budget_warning` twice per session.
    warn_fired: bool,
    /// Threshold (in elapsed session seconds) at which to emit warn.
    /// `None` if there's not enough headroom (e.g. session starts
    /// already within 60s of the cap).
    warn_at_elapsed: Option<u64>,
}

impl SessionMeter {
    /// Construct a new meter. Computes the warn threshold once; the
    /// poll path is then branch-light.
    pub fn start(cfg: &VoiceConfig, start_seconds_used: u64, started_at: Instant) -> Self {
        let cap_seconds = (cfg.budget_minutes_per_tenant_per_day as u64).saturating_mul(60);
        let max_session_seconds = cfg.max_session_seconds as u64;
        // Per design: warn 60 s before the day-budget cap.
        let warn_at_elapsed = cap_seconds
            .checked_sub(start_seconds_used)
            .and_then(|day_remaining| day_remaining.checked_sub(60))
            .filter(|w| *w > 0);
        Self {
            started_at,
            cap_seconds,
            max_session_seconds,
            start_seconds_used,
            warn_fired: false,
            warn_at_elapsed,
        }
    }

    /// Elapsed wall-clock seconds since `start`.
    pub fn elapsed_secs(&self, now: Instant) -> u64 {
        now.saturating_duration_since(self.started_at).as_secs()
    }

    /// Drive the meter at `now`. Must be called by the per-session
    /// 1-Hz ticker.
    pub fn poll(&mut self, now: Instant) -> MeterTick {
        let elapsed = self.elapsed_secs(now);

        // 1. Hard kill — independent of day budget.
        if elapsed >= self.max_session_seconds {
            return MeterTick::Terminate {
                reason: TerminateReason::MaxSessionSeconds,
                close_code: CLOSE_CODE_MAX_SESSION,
            };
        }

        // 2. Day budget cap.
        let day_used = self.start_seconds_used.saturating_add(elapsed);
        if self.cap_seconds > 0 && day_used >= self.cap_seconds {
            return MeterTick::Terminate {
                reason: TerminateReason::DayBudgetExhausted,
                close_code: CLOSE_CODE_BUDGET,
            };
        }

        // 3. One-shot warn.
        if !self.warn_fired {
            if let Some(t) = self.warn_at_elapsed {
                if elapsed >= t {
                    self.warn_fired = true;
                    let minutes_remaining = self
                        .cap_seconds
                        .saturating_sub(day_used)
                        .div_ceil(60) as u32;
                    return MeterTick::BudgetWarn { minutes_remaining };
                }
            }
        }

        MeterTick::Ok
    }

    pub fn elapsed_at_start(&self) -> Duration {
        self.started_at.elapsed()
    }
}

/// WebSocket close code for "day budget exhausted mid-session". 4xxx
/// is the application range; we use 4002 per the design's "close
/// `4002 budget`" note.
pub const CLOSE_CODE_BUDGET: u16 = 4002;

/// WebSocket close code for "session length cap". The design's
/// `provider_unavailable` uses 4003; we'd reuse it for our
/// hard-kill since both are "abnormal but expected". Keeping a named
/// constant lets the iter-8 SQLite write distinguish them.
pub const CLOSE_CODE_MAX_SESSION: u16 = 4001;

// ---------------------------------------------------------------------------
// UTC day epoch helper
// ---------------------------------------------------------------------------

/// Days since UNIX epoch in UTC. The voice spend bucket key.
pub fn utc_day_epoch(unix_secs: u64) -> u64 {
    unix_secs / 86_400
}

/// Next UTC midnight as a UNIX timestamp; emitted as `reset_at` in
/// the 429 budget-exhausted body.
pub fn next_utc_midnight(unix_secs: u64) -> u64 {
    (utc_day_epoch(unix_secs) + 1) * 86_400
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn cfg_with(budget_min: u32, max_secs: u32) -> VoiceConfig {
        VoiceConfig {
            enabled: true,
            budget_minutes_per_tenant_per_day: budget_min,
            max_session_seconds: max_secs,
            ..VoiceConfig::default()
        }
    }

    // ----- DaySpend / utc helpers -----

    #[test]
    fn utc_day_epoch_rolls_at_midnight() {
        // 2026-05-08 00:00:00 UTC = 1778198400
        let midnight = 1_778_198_400u64;
        assert_eq!(utc_day_epoch(midnight), utc_day_epoch(midnight + 86_399));
        assert_ne!(utc_day_epoch(midnight), utc_day_epoch(midnight + 86_400));
    }

    #[test]
    fn next_midnight_is_today_plus_24h() {
        let now = 1_778_198_400u64 + 12 * 3600; // mid-day
        assert_eq!(next_utc_midnight(now), 1_778_198_400 + 86_400);
    }

    // ----- InMemoryVoiceSpend -----

    #[test]
    fn in_memory_spend_starts_empty() {
        let s = InMemoryVoiceSpend::new();
        let snap = s.snapshot("t1", 100);
        assert_eq!(snap.seconds_used, 0);
        assert_eq!(snap.sessions_count, 0);
        assert_eq!(snap.day_epoch, 100);
    }

    #[test]
    fn in_memory_spend_accumulates_within_day() {
        let s = InMemoryVoiceSpend::new();
        s.add_seconds("t1", 100, 30);
        s.add_seconds("t1", 100, 45);
        let snap = s.snapshot("t1", 100);
        assert_eq!(snap.seconds_used, 75);
    }

    #[test]
    fn in_memory_spend_isolates_tenants_and_days() {
        let s = InMemoryVoiceSpend::new();
        s.add_seconds("t1", 100, 30);
        s.add_seconds("t2", 100, 5);
        s.add_seconds("t1", 101, 99);
        assert_eq!(s.snapshot("t1", 100).seconds_used, 30);
        assert_eq!(s.snapshot("t2", 100).seconds_used, 5);
        assert_eq!(s.snapshot("t1", 101).seconds_used, 99);
    }

    #[test]
    fn record_session_start_increments_count() {
        let s = InMemoryVoiceSpend::new();
        let snap = s.record_session_start("t1", 100);
        assert_eq!(snap.sessions_count, 1);
        let snap = s.record_session_start("t1", 100);
        assert_eq!(snap.sessions_count, 2);
    }

    // ----- evaluate_budget -----

    #[test]
    fn budget_allows_when_well_under_cap() {
        let cfg = cfg_with(30, 600);
        let today = DaySpend {
            day_epoch: 100,
            seconds_used: 60, // 1 min used / 30 min cap
            sessions_count: 1,
        };
        let d = evaluate_budget(&cfg, today, 999);
        assert!(matches!(
            d,
            BudgetDecision::Allow { seconds_remaining } if seconds_remaining == 30 * 60 - 60
        ));
    }

    #[test]
    fn budget_denies_when_exactly_at_cap() {
        let cfg = cfg_with(30, 600);
        let today = DaySpend {
            day_epoch: 100,
            seconds_used: 30 * 60,
            sessions_count: 5,
        };
        let d = evaluate_budget(&cfg, today, 999);
        match d {
            BudgetDecision::Deny {
                reason: BudgetDenyReason::DayBudgetExhausted { used_seconds, cap_seconds },
                reset_at,
            } => {
                assert_eq!(used_seconds, 1800);
                assert_eq!(cap_seconds, 1800);
                assert_eq!(reset_at, 999);
            }
            other => panic!("expected DayBudgetExhausted; got {other:?}"),
        }
    }

    #[test]
    fn budget_denies_when_overdrawn() {
        let cfg = cfg_with(30, 600);
        let today = DaySpend {
            day_epoch: 100,
            seconds_used: 31 * 60,
            sessions_count: 5,
        };
        let d = evaluate_budget(&cfg, today, 999);
        assert!(matches!(
            d,
            BudgetDecision::Deny {
                reason: BudgetDenyReason::DayBudgetExhausted { .. },
                ..
            }
        ));
    }

    #[test]
    fn budget_denies_when_cap_is_zero() {
        // Operator zeroed the per-tenant cap as a kill-switch.
        let cfg = cfg_with(0, 600);
        let today = DaySpend::fresh(100);
        let d = evaluate_budget(&cfg, today, 999);
        assert!(matches!(
            d,
            BudgetDecision::Deny {
                reason: BudgetDenyReason::BudgetIsZero,
                reset_at: 999,
            }
        ));
    }

    // ----- SessionMeter -----

    #[test]
    fn session_meter_ok_when_fresh() {
        let cfg = cfg_with(30, 600);
        let mut m = SessionMeter::start(&cfg, 0, Instant::now());
        assert_eq!(m.poll(Instant::now()), MeterTick::Ok);
    }

    #[test]
    fn session_meter_terminates_at_max_session_seconds() {
        // max_session = 5s; jump elapsed past it.
        let cfg = cfg_with(30, 5);
        let start = Instant::now();
        let mut m = SessionMeter::start(&cfg, 0, start);
        let later = start + Duration::from_secs(6);
        match m.poll(later) {
            MeterTick::Terminate { reason, close_code } => {
                assert_eq!(reason, TerminateReason::MaxSessionSeconds);
                assert_eq!(close_code, CLOSE_CODE_MAX_SESSION);
            }
            other => panic!("expected Terminate; got {other:?}"),
        }
    }

    #[test]
    fn session_meter_terminates_when_day_budget_hits_mid_session() {
        // 30 min/day cap, but the day already has 29 min 50 s used.
        // Once elapsed crosses 10 s, the meter must terminate.
        let cfg = cfg_with(30, 3600);
        let start = Instant::now();
        let mut m = SessionMeter::start(&cfg, 29 * 60 + 50, start);
        let later = start + Duration::from_secs(15);
        match m.poll(later) {
            MeterTick::Terminate { reason, close_code } => {
                assert_eq!(reason, TerminateReason::DayBudgetExhausted);
                assert_eq!(close_code, CLOSE_CODE_BUDGET);
            }
            other => panic!("expected Terminate; got {other:?}"),
        }
    }

    #[test]
    fn session_meter_emits_warn_60s_before_cap_then_terminates() {
        // 5 min cap, no prior usage today. Session running for
        // 4m1s → warn (60s remaining). 5m0s → terminate.
        let cfg = cfg_with(5, 3600);
        let start = Instant::now();
        let mut m = SessionMeter::start(&cfg, 0, start);

        // Before warn threshold.
        assert_eq!(m.poll(start + Duration::from_secs(120)), MeterTick::Ok);

        // At warn threshold: 5*60 - 60 = 240s elapsed.
        match m.poll(start + Duration::from_secs(240)) {
            MeterTick::BudgetWarn { minutes_remaining } => {
                assert_eq!(minutes_remaining, 1);
            }
            other => panic!("expected BudgetWarn; got {other:?}"),
        }

        // Warn is one-shot: a subsequent tick before the cap is Ok.
        assert_eq!(m.poll(start + Duration::from_secs(241)), MeterTick::Ok);

        // At the cap: terminate.
        match m.poll(start + Duration::from_secs(300)) {
            MeterTick::Terminate { reason, .. } => {
                assert_eq!(reason, TerminateReason::DayBudgetExhausted);
            }
            other => panic!("expected Terminate; got {other:?}"),
        }
    }

    #[test]
    fn session_meter_skips_warn_when_cap_already_close() {
        // Session starts already within 60s of the cap → no warn
        // threshold computed (start_seconds_used already eats most of
        // the day). The meter terminates without emitting warn.
        let cfg = cfg_with(5, 3600);
        let start = Instant::now();
        // Day already burned 4m30s of the 5m cap → only 30s headroom.
        let mut m = SessionMeter::start(&cfg, 4 * 60 + 30, start);

        // The first tick after the session would burn the remainder
        // and terminate; never sees BudgetWarn.
        let tick_a = m.poll(start + Duration::from_secs(15));
        let tick_b = m.poll(start + Duration::from_secs(35));
        assert_eq!(tick_a, MeterTick::Ok);
        assert!(matches!(tick_b, MeterTick::Terminate { .. }));
    }
}
