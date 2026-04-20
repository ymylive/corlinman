//! Retry backoff schedule shared by agent-client and plugin async-task polling.
//!
//! Matches openclaw's `BACKOFF_SCHEDULE_MS` (plan §8 A3): `[5s, 10s, 30s, 60s]`.
//! Non-retryable reasons (`AuthPermanent`, `ModelNotFound`, `ContextOverflow`)
//! short-circuit [`next_delay`] to `None` on attempt 0.

use std::time::Duration;

use crate::error::FailoverReason;

/// Default retry schedule — four attempts with exponential-ish spacing.
///
/// Attempt 0 → wait `SCHEDULE[0]` before retry; attempt `SCHEDULE.len()`
/// signals "give up".
pub const DEFAULT_SCHEDULE: [Duration; 4] = [
    Duration::from_secs(5),
    Duration::from_secs(10),
    Duration::from_secs(30),
    Duration::from_secs(60),
];

/// Return the backoff for the next retry, or `None` if the caller should stop.
///
/// `attempt` is the zero-based index of the *next* attempt (so 0 means "I have
/// failed once, how long until I try again"). `reason` short-circuits when
/// non-retryable.
pub fn next_delay(attempt: usize, reason: FailoverReason) -> Option<Duration> {
    if !reason.retryable() {
        return None;
    }
    DEFAULT_SCHEDULE.get(attempt).copied()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schedule_matches_openclaw() {
        let secs: Vec<u64> = DEFAULT_SCHEDULE.iter().map(|d| d.as_secs()).collect();
        assert_eq!(secs, vec![5, 10, 30, 60]);
    }

    #[test]
    fn non_retryable_returns_none() {
        assert!(next_delay(0, FailoverReason::AuthPermanent).is_none());
        assert!(next_delay(0, FailoverReason::ModelNotFound).is_none());
        assert!(next_delay(0, FailoverReason::ContextOverflow).is_none());
    }

    #[test]
    fn overflow_returns_none() {
        assert!(next_delay(4, FailoverReason::RateLimit).is_none());
        assert!(next_delay(100, FailoverReason::Unknown).is_none());
    }

    #[test]
    fn rate_limit_respects_schedule() {
        assert_eq!(
            next_delay(0, FailoverReason::RateLimit),
            Some(Duration::from_secs(5))
        );
        assert_eq!(
            next_delay(3, FailoverReason::RateLimit),
            Some(Duration::from_secs(60))
        );
    }
}
