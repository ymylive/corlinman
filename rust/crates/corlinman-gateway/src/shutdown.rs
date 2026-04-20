//! Graceful shutdown: SIGTERM / SIGINT → drain in-flight → exit code 143.
//!
//! The gateway process obeys POSIX convention: on SIGTERM / SIGINT it
//! stops accepting new connections, lets in-flight requests finish, then exits
//! with status **143** (the Unix convention for `128 + SIGTERM`). Docker and
//! systemd read that exit code as a clean stop.

use tokio::signal;

/// Reason the shutdown was triggered. Surfaced to callers in case they want
/// to differentiate log messages or shortcut specific cleanup paths.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShutdownReason {
    /// SIGTERM (docker stop / systemd / kill).
    Terminate,
    /// SIGINT (Ctrl-C).
    Interrupt,
}

/// Returns when the first SIGTERM or SIGINT arrives. Errors from the signal
/// subsystem are logged and surfaced as `ShutdownReason::Terminate` (the
/// conservative default — we'd rather exit than hang).
pub async fn wait_for_signal() -> ShutdownReason {
    #[cfg(unix)]
    {
        use signal::unix::{signal, SignalKind};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(s) => s,
            Err(err) => {
                tracing::warn!(error = %err, "failed to register SIGTERM handler");
                return ShutdownReason::Terminate;
            }
        };
        let mut int = match signal(SignalKind::interrupt()) {
            Ok(s) => s,
            Err(err) => {
                tracing::warn!(error = %err, "failed to register SIGINT handler");
                return ShutdownReason::Interrupt;
            }
        };
        tokio::select! {
            _ = term.recv() => ShutdownReason::Terminate,
            _ = int.recv() => ShutdownReason::Interrupt,
        }
    }

    #[cfg(not(unix))]
    {
        // Windows: Ctrl-C only; no SIGTERM equivalent.
        if let Err(err) = signal::ctrl_c().await {
            tracing::warn!(error = %err, "failed to wait for Ctrl-C");
        }
        ShutdownReason::Interrupt
    }
}

/// Exit code following POSIX convention (128 + SIGTERM = 143).
pub const EXIT_CODE_ON_SIGNAL: i32 = 143;
