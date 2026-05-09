//! Mid-session budget enforcement + checkpoint ticker.
//!
//! Iter 8 of D4. Wraps the iter-3 [`cost`] primitives into a single
//! per-session enforcer that:
//!
//! 1. Drives a 1-Hz tick loop the route handler uses as its budget
//!    polling cadence.
//! 2. **Checkpoints in-flight seconds to the [`VoiceSpend`] store on
//!    every tick** so a gateway crash mid-session can't leak unbilled
//!    minutes. Iter 3 only wrote spend at session-end; that lost up to
//!    600s of usage on every crash, which is exactly the kind of
//!    silent leak the design's three-layer cost gate exists to prevent.
//! 3. Returns a [`BudgetTickAction`] enum the handler maps to
//!    server-side side-effects:
//!    - `Continue`: nothing to do, keep going.
//!    - `EmitWarning { minutes_remaining }`: send a `budget_warning`
//!      JSON frame to the client.
//!    - `Terminate { reason, close_code }`: emit a final `error`
//!      frame, then close the WebSocket with the supplied code.
//! 4. On graceful close, [`BudgetEnforcer::finalize`] flushes the last
//!    delta to the spend store so the day-budget total includes every
//!    second the session was open.
//!
//! ## Why a separate type?
//!
//! [`super::cost::SessionMeter`] is pure (no I/O). The enforcer adds
//! the single-source-of-truth checkpoint write so the spend store is
//! always within ~1s of the live session's accumulated usage. That
//! property makes the iter-9 hot-path bridge's crash-safety story
//! trivial: no special "session was killed" recovery logic needed —
//! the next session start reads a spend value that already
//! incorporates everything the dying session billed.

use std::sync::Arc;
use std::time::Instant;

use corlinman_core::config::VoiceConfig;
use tracing::debug;

use super::cost::{MeterTick, SessionMeter, TerminateReason, VoiceSpend};

/// Result of one [`BudgetEnforcer::tick`] call. Maps 1:1 onto the side
/// effects the route handler must perform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetTickAction {
    /// Healthy session; no action required.
    Continue,
    /// Emit a `budget_warning` server frame with the supplied
    /// `minutes_remaining` field. Fires once per session at
    /// approximately 60s before the day-budget cap.
    EmitWarning { minutes_remaining: u32 },
    /// Close the session. The handler:
    /// 1. Emits an `error` server frame describing the cause,
    /// 2. Sends a `Close` frame with `close_code`,
    /// 3. Records `voice_sessions.end_reason` from `reason`.
    Terminate {
        reason: TerminateReason,
        close_code: u16,
    },
}

/// Per-session budget enforcer. Owns:
///
/// - the configured tenant slug + day_epoch (immutable for the
///   session — a session that crosses midnight UTC keeps the same
///   day_epoch so the day's row stays consistent),
/// - the [`SessionMeter`] (pure timing logic),
/// - an `Arc<dyn VoiceSpend>` handle for checkpoint writes,
/// - the cumulative seconds already-billed-back-to-the-store, so each
///   tick only writes the **delta** from the previous tick (avoids
///   double-counting if the spend store already has prior session
///   contribution).
pub struct BudgetEnforcer {
    spend: Arc<dyn VoiceSpend>,
    tenant: String,
    day_epoch: u64,
    meter: SessionMeter,
    /// Wall-clock anchor for elapsed-second math. Same instant the
    /// meter was constructed against — we reuse it so the two views
    /// stay in lock-step.
    started_at: Instant,
    /// Cumulative seconds that have already been written to the spend
    /// store for *this* session. The first tick writes the entire
    /// elapsed value; later ticks write `current_elapsed -
    /// last_checkpointed`.
    last_checkpointed: u64,
}

impl BudgetEnforcer {
    /// Construct a new enforcer at session start. Reads the start-time
    /// snapshot of `seconds_used` from the spend store so the meter's
    /// per-day arithmetic is anchored against today's existing usage.
    ///
    /// `started_at` should be the same `Instant` recorded against
    /// `voice_sessions.started_at`; the enforcer derives elapsed
    /// seconds from it.
    pub fn start(
        cfg: &VoiceConfig,
        spend: Arc<dyn VoiceSpend>,
        tenant: String,
        day_epoch: u64,
        started_at: Instant,
    ) -> Self {
        let snap = spend.snapshot(&tenant, day_epoch);
        let meter = SessionMeter::start(cfg, snap.seconds_used, started_at);
        Self {
            spend,
            tenant,
            day_epoch,
            meter,
            started_at,
            last_checkpointed: 0,
        }
    }

    /// Drive the enforcer at `now`. The handler's 1-Hz ticker calls
    /// this once per second. Each call:
    ///
    /// 1. Checkpoints any new elapsed seconds into the spend store
    ///    (delta-only; never re-writes already-billed seconds).
    /// 2. Polls the [`SessionMeter`] to decide whether to emit a
    ///    warning or terminate.
    pub fn tick(&mut self, now: Instant) -> BudgetTickAction {
        let elapsed = self.meter.elapsed_secs(now);
        self.checkpoint_delta(elapsed);

        match self.meter.poll(now) {
            MeterTick::Ok => BudgetTickAction::Continue,
            MeterTick::BudgetWarn { minutes_remaining } => {
                BudgetTickAction::EmitWarning { minutes_remaining }
            }
            MeterTick::Terminate { reason, close_code } => {
                BudgetTickAction::Terminate { reason, close_code }
            }
        }
    }

    /// Force a final checkpoint flush at session close. Called from
    /// the handler's clean-shutdown path (graceful end, provider
    /// error, client disconnect, or after a `Terminate` action) so
    /// the spend store includes every second the session was alive.
    ///
    /// Returns the total seconds attributed to this session — the
    /// caller writes this into `voice_sessions.duration_secs`.
    pub fn finalize(&mut self, now: Instant) -> u64 {
        let elapsed = self.meter.elapsed_secs(now);
        self.checkpoint_delta(elapsed);
        elapsed
    }

    /// Pure helper — adds (`elapsed - last_checkpointed`) seconds to
    /// the spend store and advances the cursor. A `delta` of 0 is a
    /// no-op (sub-second elapsed change between ticks).
    fn checkpoint_delta(&mut self, elapsed: u64) {
        let delta = elapsed.saturating_sub(self.last_checkpointed);
        if delta == 0 {
            return;
        }
        // The spend store's `add_seconds` is process-local under
        // `InMemoryVoiceSpend`; iter 3+ tests confirm it's safe to
        // call on a hot path. Iter 8's SQLite impl (follow-on) lands
        // a write-coalescer if profiling shows tick rate matters.
        let snap = self.spend.add_seconds(&self.tenant, self.day_epoch, delta);
        debug!(
            target: "voice",
            tenant = %self.tenant,
            day_epoch = self.day_epoch,
            delta,
            day_total = snap.seconds_used,
            "voice budget checkpoint"
        );
        self.last_checkpointed = elapsed;
    }

    /// Elapsed seconds since session start, rounded down. Exposed so
    /// the handler can stamp `voice_sessions.duration_secs` without
    /// re-deriving it.
    pub fn elapsed_secs(&self, now: Instant) -> u64 {
        self.meter.elapsed_secs(now)
    }

    /// The wall-clock anchor — handed back to the handler for
    /// callers that need to compute their own elapsed math (e.g.
    /// rolling per-frame backpressure on the audio pump).
    pub fn started_at(&self) -> Instant {
        self.started_at
    }

    /// Test seam — peek at the cumulative seconds already written to
    /// the store. Used by tick tests to assert the delta-only write
    /// invariant without reaching into private fields.
    #[cfg(test)]
    pub(crate) fn last_checkpointed(&self) -> u64 {
        self.last_checkpointed
    }
}

/// Map a [`TerminateReason`] to its persisted `voice_sessions
/// .end_reason` string. Co-located with the reason enum so the
/// handler doesn't have to know the column name.
pub fn terminate_reason_to_end_reason(r: TerminateReason) -> &'static str {
    match r {
        TerminateReason::DayBudgetExhausted => "budget",
        TerminateReason::MaxSessionSeconds => "max_session",
    }
}

/// Map a [`TerminateReason`] to a human-readable error message — the
/// handler emits this verbatim in the final `error` server frame.
pub fn terminate_reason_to_message(r: TerminateReason) -> &'static str {
    match r {
        TerminateReason::DayBudgetExhausted => {
            "daily voice budget exhausted; session terminated"
        }
        TerminateReason::MaxSessionSeconds => {
            "session length cap reached; session terminated"
        }
    }
}

/// Map a [`TerminateReason`] to a stable error-code string. The
/// `error.code` JSON field; clients pattern-match on this.
pub fn terminate_reason_to_code(r: TerminateReason) -> &'static str {
    match r {
        TerminateReason::DayBudgetExhausted => "budget_exhausted",
        TerminateReason::MaxSessionSeconds => "max_session_reached",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    use super::super::cost::{InMemoryVoiceSpend, CLOSE_CODE_BUDGET, CLOSE_CODE_MAX_SESSION};

    fn cfg(budget_min: u32, max_secs: u32) -> VoiceConfig {
        VoiceConfig {
            enabled: true,
            budget_minutes_per_tenant_per_day: budget_min,
            max_session_seconds: max_secs,
            ..VoiceConfig::default()
        }
    }

    #[test]
    fn first_tick_with_no_elapsed_is_noop() {
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "t1".into(), 100, started);

        // Same instant → 0 elapsed → no checkpoint write.
        let action = e.tick(started);
        assert_eq!(action, BudgetTickAction::Continue);
        let snap = spend.snapshot("t1", 100);
        assert_eq!(snap.seconds_used, 0, "no delta should be written at t=0");
        assert_eq!(e.last_checkpointed(), 0);
    }

    #[test]
    fn tick_writes_delta_only() {
        // Two ticks, 5s apart. The store must accumulate exactly
        // (5 - 0) + (10 - 5) = 10 seconds, NOT 5 + 10 = 15.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "t1".into(), 100, started);

        let _ = e.tick(started + Duration::from_secs(5));
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 5);
        assert_eq!(e.last_checkpointed(), 5);

        let _ = e.tick(started + Duration::from_secs(10));
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 10);
        assert_eq!(e.last_checkpointed(), 10);
    }

    #[test]
    fn warn_fires_60s_before_cap() {
        // 5 min cap, no prior usage. Warn should fire at ~4m elapsed
        // (cap_seconds - 60). Pinning the action variant pins the
        // round-trip from cost::MeterTick to BudgetTickAction.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(5, 3600), spend, "t1".into(), 100, started);

        // 2 min in — still Continue.
        assert_eq!(
            e.tick(started + Duration::from_secs(120)),
            BudgetTickAction::Continue
        );

        // Warn at exactly the threshold.
        let action = e.tick(started + Duration::from_secs(240));
        match action {
            BudgetTickAction::EmitWarning { minutes_remaining } => {
                assert_eq!(minutes_remaining, 1);
            }
            other => panic!("expected EmitWarning; got {other:?}"),
        }

        // Warn is one-shot per the cost::SessionMeter contract.
        assert_eq!(
            e.tick(started + Duration::from_secs(241)),
            BudgetTickAction::Continue
        );
    }

    #[test]
    fn terminate_at_day_budget_cap() {
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(5, 3600), spend.clone(), "t1".into(), 100, started);

        // At 300 s (cap), terminate fires.
        let action = e.tick(started + Duration::from_secs(300));
        match action {
            BudgetTickAction::Terminate { reason, close_code } => {
                assert_eq!(reason, TerminateReason::DayBudgetExhausted);
                assert_eq!(close_code, CLOSE_CODE_BUDGET);
            }
            other => panic!("expected Terminate; got {other:?}"),
        }
        // Tick wrote the elapsed delta even though terminate was
        // returned — the design says the spend store must reflect
        // every billed second, including the one that tripped the
        // cap.
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 300);
    }

    #[test]
    fn terminate_at_max_session_seconds_uses_distinct_close_code() {
        // max_session=5 with a 30-min budget — the session-length cap
        // hits first; the close code distinguishes "operator's
        // wallet" from "stuck session" in the close-code emission.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(30, 5), spend, "t1".into(), 100, started);

        let action = e.tick(started + Duration::from_secs(6));
        match action {
            BudgetTickAction::Terminate { reason, close_code } => {
                assert_eq!(reason, TerminateReason::MaxSessionSeconds);
                assert_eq!(close_code, CLOSE_CODE_MAX_SESSION);
            }
            other => panic!("expected Terminate; got {other:?}"),
        }
    }

    #[test]
    fn finalize_flushes_remaining_delta() {
        // Session ran 10 s, the last tick was at 7 s. finalize() at
        // 10 s must add exactly 3 s to the store so the day total
        // covers the full session.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "t1".into(), 100, started);

        let _ = e.tick(started + Duration::from_secs(7));
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 7);

        let final_secs = e.finalize(started + Duration::from_secs(10));
        assert_eq!(final_secs, 10);
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 10);
        // Idempotent: calling finalize again at the same instant must
        // not double-write.
        let _ = e.finalize(started + Duration::from_secs(10));
        assert_eq!(spend.snapshot("t1", 100).seconds_used, 10);
    }

    #[test]
    fn finalize_with_mid_session_terminate_does_not_double_count() {
        // The handler calls finalize() AFTER acting on a Terminate
        // tick. The terminate tick already checkpointed; finalize at
        // the same instant must be a no-op for the store.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(5, 3600), spend.clone(), "t1".into(), 100, started);

        // Terminate at 300 s flushes 300 to the store.
        let _ = e.tick(started + Duration::from_secs(300));
        let after_terminate = spend.snapshot("t1", 100).seconds_used;

        // finalize at the same instant adds 0.
        let final_secs = e.finalize(started + Duration::from_secs(300));
        assert_eq!(final_secs, 300);
        assert_eq!(spend.snapshot("t1", 100).seconds_used, after_terminate);
    }

    #[test]
    fn checkpoint_carries_existing_day_usage() {
        // Pre-populate the store with 10 minutes of prior session
        // usage. New session adds another 30 s. The store must show
        // 600 + 30 — never overwriting the prior session's
        // contribution.
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        spend.add_seconds("t1", 100, 600);

        let started = Instant::now();
        let mut e = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "t1".into(), 100, started);
        let _ = e.tick(started + Duration::from_secs(30));

        let snap = spend.snapshot("t1", 100);
        assert_eq!(snap.seconds_used, 630, "must add to prior usage, not overwrite");
    }

    #[test]
    fn enforcer_is_per_tenant_isolated() {
        let spend: Arc<dyn VoiceSpend> = Arc::new(InMemoryVoiceSpend::new());
        let started = Instant::now();

        // Two tenants run concurrently against the same spend store.
        let mut a = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "a".into(), 100, started);
        let mut b = BudgetEnforcer::start(&cfg(30, 600), spend.clone(), "b".into(), 100, started);

        let _ = a.tick(started + Duration::from_secs(5));
        let _ = b.tick(started + Duration::from_secs(7));

        assert_eq!(spend.snapshot("a", 100).seconds_used, 5);
        assert_eq!(spend.snapshot("b", 100).seconds_used, 7);
    }

    #[test]
    fn terminate_reason_strings_are_stable() {
        // Pinned: persisted column value. Renaming a TerminateReason
        // variant without updating these mappings would create
        // unparseable rows in the voice_sessions table for older DBs.
        assert_eq!(
            terminate_reason_to_end_reason(TerminateReason::DayBudgetExhausted),
            "budget"
        );
        assert_eq!(
            terminate_reason_to_end_reason(TerminateReason::MaxSessionSeconds),
            "max_session"
        );
        assert_eq!(
            terminate_reason_to_code(TerminateReason::DayBudgetExhausted),
            "budget_exhausted"
        );
        assert_eq!(
            terminate_reason_to_code(TerminateReason::MaxSessionSeconds),
            "max_session_reached"
        );
        assert!(!terminate_reason_to_message(TerminateReason::DayBudgetExhausted).is_empty());
        assert!(!terminate_reason_to_message(TerminateReason::MaxSessionSeconds).is_empty());
    }
}
