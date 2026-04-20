//! Error taxonomy for every corlinman Rust crate.
//!
//! `CorlinmanError` is the single fallible return type for library code;
//! gateway edges (axum handlers, gRPC services) convert it into HTTP / gRPC
//! status codes via [`CorlinmanError::status_code`] and
//! [`CorlinmanError::grpc_code`].
//!
//! `FailoverReason` is the classification used by `corlinman-agent-client`
//! and the Python `corlinman_providers.failover` module. The variants and
//! their discriminants mirror `FailoverReason` in `proto/common.proto`.

use http::StatusCode;
use thiserror::Error;

/// Classified failure reason used for provider failover + retry decisions.
///
/// Mirrors `corlinman.v1.FailoverReason` in the proto file; the variant order
/// is load-bearing because both sides cast to/from the proto integer.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(i32)]
pub enum FailoverReason {
    #[default]
    Unspecified = 0,
    /// Quota exhausted, card declined, org billing disabled.
    Billing = 1,
    /// 429; retryable after provider-advertised backoff.
    RateLimit = 2,
    /// 401/403 that may be transient (key rotation). Retry once.
    Auth = 3,
    /// Revoked or structurally invalid key. **Do not retry.**
    AuthPermanent = 4,
    /// Upstream did not respond within deadline.
    Timeout = 5,
    /// 404 on model id / provider doesn't host this model.
    ModelNotFound = 6,
    /// Malformed response / JSON parse failed / SSE framing broken.
    Format = 7,
    /// Prompt exceeds provider context window.
    ContextOverflow = 8,
    /// 503 / "overloaded_error" (anthropic).
    Overloaded = 9,
    /// Catch-all; treat as retryable **once**.
    Unknown = 10,
}

impl FailoverReason {
    /// Whether a caller should retry (possibly after backoff).
    ///
    /// `AuthPermanent` never retries; `Auth` retries exactly once; everything
    /// else respects the usual backoff schedule.
    pub fn retryable(self) -> bool {
        !matches!(
            self,
            Self::AuthPermanent | Self::ModelNotFound | Self::ContextOverflow
        )
    }

    /// Human-readable label used in metrics + structured logs.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unspecified => "unspecified",
            Self::Billing => "billing",
            Self::RateLimit => "rate_limit",
            Self::Auth => "auth",
            Self::AuthPermanent => "auth_permanent",
            Self::Timeout => "timeout",
            Self::ModelNotFound => "model_not_found",
            Self::Format => "format",
            Self::ContextOverflow => "context_overflow",
            Self::Overloaded => "overloaded",
            Self::Unknown => "unknown",
        }
    }
}

/// The canonical corlinman error type.
///
/// Use `#[from]` conversions from `serde_json::Error`, `std::io::Error`, and
/// `regex::Error`; wrap upstream provider errors in `Upstream { reason, msg }`
/// so the failover layer can classify without re-parsing.
#[derive(Debug, Error)]
pub enum CorlinmanError {
    /// Configuration invalid or missing required key.
    #[error("config error: {0}")]
    Config(String),

    /// Validation failure (schema / bounds / regex). `path` is a JSON-pointer-ish locator.
    #[error("validation failed at {path}: {message}")]
    Validation { path: String, message: String },

    /// Placeholder / tool-request / channel-binding parse failure.
    #[error("parse error ({what}): {message}")]
    Parse { what: &'static str, message: String },

    /// Requested entity (plugin, agent, model, chunk) not present.
    #[error("not found: {kind} '{id}'")]
    NotFound { kind: &'static str, id: String },

    /// Caller not authenticated (no / bad AdminPassword, missing API key).
    #[error("unauthenticated")]
    Unauthenticated,

    /// Caller authenticated but not allowed (approval denied, scope mismatch).
    #[error("permission denied: {0}")]
    PermissionDenied(String),

    /// Operation was cancelled (CancellationToken fired / client disconnect).
    #[error("cancelled: {0}")]
    Cancelled(&'static str),

    /// Timed out before upstream responded.
    #[error("timeout after {millis}ms: {what}")]
    Timeout { what: &'static str, millis: u64 },

    /// Upstream provider / gRPC peer returned a classifiable failure.
    #[error("upstream {reason:?}: {message}")]
    Upstream {
        reason: FailoverReason,
        message: String,
    },

    /// Plugin runtime failure (sandbox OOM, non-zero exit, JSON-RPC protocol break).
    #[error("plugin '{plugin}' runtime error: {message}")]
    PluginRuntime { plugin: String, message: String },

    /// Vector / SQLite / usearch storage error.
    #[error("storage error: {0}")]
    Storage(String),

    /// I/O (file system, network socket) error. Prefer wrapping via `#[from]`.
    #[error("io: {0}")]
    Io(#[from] std::io::Error),

    /// JSON (de)serialisation error.
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),

    /// Regex compile error (only triggered at startup for baked-in patterns).
    #[error("regex: {0}")]
    Regex(#[from] regex::Error),

    /// Fallback when bubbling up an `anyhow::Error` from an edge layer.
    #[error("internal: {0}")]
    Internal(String),
}

impl CorlinmanError {
    /// Map the error to an HTTP status for axum / admin REST responses.
    ///
    /// Upstream errors collapse into 502/504/429 based on their
    /// `FailoverReason`; everything else maps 1:1.
    pub fn status_code(&self) -> StatusCode {
        match self {
            Self::Config(_) | Self::Validation { .. } | Self::Parse { .. } => {
                StatusCode::BAD_REQUEST
            }
            Self::NotFound { .. } => StatusCode::NOT_FOUND,
            Self::Unauthenticated => StatusCode::UNAUTHORIZED,
            Self::PermissionDenied(_) => StatusCode::FORBIDDEN,
            Self::Cancelled(_) => StatusCode::from_u16(499).unwrap_or(StatusCode::BAD_REQUEST),
            Self::Timeout { .. } => StatusCode::GATEWAY_TIMEOUT,
            Self::Upstream { reason, .. } => match reason {
                FailoverReason::RateLimit => StatusCode::TOO_MANY_REQUESTS,
                FailoverReason::Timeout => StatusCode::GATEWAY_TIMEOUT,
                FailoverReason::Auth | FailoverReason::AuthPermanent => StatusCode::UNAUTHORIZED,
                FailoverReason::ModelNotFound => StatusCode::NOT_FOUND,
                FailoverReason::ContextOverflow => StatusCode::PAYLOAD_TOO_LARGE,
                FailoverReason::Overloaded => StatusCode::SERVICE_UNAVAILABLE,
                FailoverReason::Billing => StatusCode::PAYMENT_REQUIRED,
                FailoverReason::Format | FailoverReason::Unknown | FailoverReason::Unspecified => {
                    StatusCode::BAD_GATEWAY
                }
            },
            Self::PluginRuntime { .. } => StatusCode::BAD_GATEWAY,
            Self::Storage(_) | Self::Io(_) | Self::Json(_) | Self::Regex(_) | Self::Internal(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        }
    }

    /// Map to a tonic / gRPC status code. Kept as a `u16` (tonic's `Code as i32`
    /// range) to avoid a hard dep on `tonic` from this crate.
    pub fn grpc_code(&self) -> i32 {
        // Values match `tonic::Code` at the time of writing.
        match self {
            Self::Config(_) | Self::Validation { .. } | Self::Parse { .. } => 3, // InvalidArgument
            Self::NotFound { .. } => 5,                                          // NotFound
            Self::Unauthenticated => 16,                                         // Unauthenticated
            Self::PermissionDenied(_) => 7,                                      // PermissionDenied
            Self::Cancelled(_) => 1,                                             // Cancelled
            Self::Timeout { .. } => 4,                                           // DeadlineExceeded
            Self::Upstream { reason, .. } => match reason {
                FailoverReason::RateLimit => 8, // ResourceExhausted
                FailoverReason::Timeout => 4,   // DeadlineExceeded
                FailoverReason::Auth | FailoverReason::AuthPermanent => 16,
                FailoverReason::ModelNotFound => 5,
                FailoverReason::ContextOverflow => 3, // InvalidArgument
                FailoverReason::Overloaded => 14,     // Unavailable
                FailoverReason::Billing => 8,         // ResourceExhausted
                _ => 13,                              // Internal
            },
            _ => 13, // Internal
        }
    }

    /// Short machine-readable code for JSON error bodies (`{"code": ...}`).
    pub fn code(&self) -> &'static str {
        match self {
            Self::Config(_) => "config",
            Self::Validation { .. } => "validation",
            Self::Parse { .. } => "parse",
            Self::NotFound { .. } => "not_found",
            Self::Unauthenticated => "unauthenticated",
            Self::PermissionDenied(_) => "permission_denied",
            Self::Cancelled(_) => "cancelled",
            Self::Timeout { .. } => "timeout",
            Self::Upstream { .. } => "upstream",
            Self::PluginRuntime { .. } => "plugin_runtime",
            Self::Storage(_) => "storage",
            Self::Io(_) => "io",
            Self::Json(_) => "json",
            Self::Regex(_) => "regex",
            Self::Internal(_) => "internal",
        }
    }

    /// Whether the caller should retry after backoff.
    pub fn retryable(&self) -> bool {
        match self {
            Self::Upstream { reason, .. } => reason.retryable(),
            Self::Timeout { .. } | Self::Io(_) => true,
            _ => false,
        }
    }
}

impl From<anyhow::Error> for CorlinmanError {
    fn from(err: anyhow::Error) -> Self {
        // Preserve chain via Display; downcast is up to the edge layer if needed.
        Self::Internal(format!("{err:#}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upstream_rate_limit_maps_to_429() {
        let e = CorlinmanError::Upstream {
            reason: FailoverReason::RateLimit,
            message: "slow down".into(),
        };
        assert_eq!(e.status_code(), StatusCode::TOO_MANY_REQUESTS);
        assert!(e.retryable());
    }

    #[test]
    fn auth_permanent_is_not_retryable() {
        assert!(!FailoverReason::AuthPermanent.retryable());
    }

    #[test]
    fn validation_maps_to_400() {
        let e = CorlinmanError::Validation {
            path: "$.port".into(),
            message: "out of range".into(),
        };
        assert_eq!(e.status_code(), StatusCode::BAD_REQUEST);
    }
}
