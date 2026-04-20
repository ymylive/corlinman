//! Upstream error → `FailoverReason` classifier (plan §8 A1).
//!
//! Maps the tonic `Status::code()` space onto `FailoverReason`. Edge gateways
//! and retry loops consume the output to decide whether to fail over to the
//! next provider or surface the error directly.

use corlinman_core::FailoverReason;
use tonic::{Code, Status};

/// Classify a `tonic::Status` into a [`FailoverReason`].
///
/// The mapping aligns with `corlinman-core::error::CorlinmanError::grpc_code`
/// so that a round-trip (Rust enum → status → classify) is stable.
pub fn classify_grpc_error(status: &Status) -> FailoverReason {
    match status.code() {
        Code::Ok => FailoverReason::Unspecified,
        // 429-equivalents — retryable.
        Code::ResourceExhausted => FailoverReason::RateLimit,
        // Upstream timeout / deadline.
        Code::DeadlineExceeded => FailoverReason::Timeout,
        // Service unavailable / overloaded.
        Code::Unavailable => FailoverReason::Overloaded,
        // Auth problem — treat as `Auth` (transient); callers decide whether to
        // escalate to `AuthPermanent` based on upstream_code string.
        Code::Unauthenticated => classify_auth(status.message()),
        Code::PermissionDenied => FailoverReason::AuthPermanent,
        // Not found: 404 on model / plugin.
        Code::NotFound => FailoverReason::ModelNotFound,
        // Malformed input.
        Code::InvalidArgument => FailoverReason::Format,
        // Provider cancelled / client cancelled — not retryable.
        Code::Cancelled => FailoverReason::Unspecified,
        // Catch-all: retry once.
        _ => FailoverReason::Unknown,
    }
}

/// Heuristic that flips `Auth` to `AuthPermanent` when the upstream message
/// hints at a hard revocation (keyword list kept small to avoid false positives).
fn classify_auth(message: &str) -> FailoverReason {
    let lower = message.to_lowercase();
    if lower.contains("revoked") || lower.contains("invalid_api_key") || lower.contains("permanent")
    {
        FailoverReason::AuthPermanent
    } else {
        FailoverReason::Auth
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resource_exhausted_is_rate_limit() {
        let s = Status::resource_exhausted("quota");
        assert_eq!(classify_grpc_error(&s), FailoverReason::RateLimit);
    }

    #[test]
    fn deadline_is_timeout() {
        let s = Status::deadline_exceeded("slow");
        assert_eq!(classify_grpc_error(&s), FailoverReason::Timeout);
    }

    #[test]
    fn unavailable_is_overloaded() {
        let s = Status::unavailable("backpressure");
        assert_eq!(classify_grpc_error(&s), FailoverReason::Overloaded);
    }

    #[test]
    fn unauthenticated_default_is_auth() {
        let s = Status::unauthenticated("token expired");
        assert_eq!(classify_grpc_error(&s), FailoverReason::Auth);
    }

    #[test]
    fn unauthenticated_revoked_is_permanent() {
        let s = Status::unauthenticated("invalid_api_key: key revoked");
        assert_eq!(classify_grpc_error(&s), FailoverReason::AuthPermanent);
    }

    #[test]
    fn not_found_is_model_not_found() {
        let s = Status::not_found("no such model");
        assert_eq!(classify_grpc_error(&s), FailoverReason::ModelNotFound);
    }

    #[test]
    fn invalid_argument_is_format() {
        let s = Status::invalid_argument("bad json");
        assert_eq!(classify_grpc_error(&s), FailoverReason::Format);
    }

    #[test]
    fn internal_is_unknown() {
        let s = Status::internal("boom");
        assert_eq!(classify_grpc_error(&s), FailoverReason::Unknown);
    }
}
