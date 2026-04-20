//! Retry orchestration driven by `corlinman_core::backoff::DEFAULT_SCHEDULE`.
//!
//! For M1/M2 we only need a simple "classify status → consult schedule → sleep"
//! helper; richer metrics + span attributes land with the full observability
//! stack in M7.

use std::future::Future;
use std::time::Duration;

use corlinman_core::{backoff, CorlinmanError, FailoverReason};
use tonic::Status;

use crate::classify::classify_grpc_error;

/// Inspect a failed gRPC call and decide how long (if at all) to wait before
/// retrying. Returns `Some(delay)` when the caller should retry, `None` when
/// the error is terminal.
pub fn next_retry_delay(attempt: usize, status: &Status) -> Option<(Duration, FailoverReason)> {
    let reason = classify_grpc_error(status);
    backoff::next_delay(attempt, reason).map(|d| (d, reason))
}

/// Convert a `tonic::Status` into a `CorlinmanError::Upstream` with the
/// correct `FailoverReason`. Kept here so callers don't need to know about
/// `classify`.
pub fn status_to_error(status: Status) -> CorlinmanError {
    let reason = classify_grpc_error(&status);
    CorlinmanError::Upstream {
        reason,
        message: status.message().to_string(),
    }
}

/// Run `op` with up to `DEFAULT_SCHEDULE.len() + 1` attempts, sleeping according
/// to the retry schedule between failures.
///
/// The operation is `FnMut() -> Future<Output = Result<T, Status>>` so the
/// caller can rebuild streams / channels per attempt.
pub async fn with_retry<T, F, Fut>(mut op: F) -> Result<T, CorlinmanError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, Status>>,
{
    let mut attempt = 0usize;
    loop {
        match op().await {
            Ok(v) => return Ok(v),
            Err(status) => match next_retry_delay(attempt, &status) {
                Some((delay, reason)) => {
                    tracing::warn!(
                        attempt,
                        ?reason,
                        delay_ms = delay.as_millis() as u64,
                        "agent-client retrying after failure",
                    );
                    tokio::time::sleep(delay).await;
                    attempt += 1;
                }
                None => return Err(status_to_error(status)),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_delay_for_rate_limit() {
        let s = Status::resource_exhausted("slow");
        let (delay, reason) = next_retry_delay(0, &s).expect("retryable");
        assert_eq!(delay, Duration::from_secs(5));
        assert_eq!(reason, FailoverReason::RateLimit);
    }

    #[test]
    fn retry_delay_returns_none_for_not_found() {
        let s = Status::not_found("model gone");
        assert!(next_retry_delay(0, &s).is_none());
    }

    #[test]
    fn retry_delay_exhausted() {
        let s = Status::unavailable("down");
        assert!(next_retry_delay(4, &s).is_none());
    }

    #[test]
    fn status_to_error_preserves_reason() {
        let s = Status::resource_exhausted("x");
        match status_to_error(s) {
            CorlinmanError::Upstream { reason, .. } => {
                assert_eq!(reason, FailoverReason::RateLimit);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
