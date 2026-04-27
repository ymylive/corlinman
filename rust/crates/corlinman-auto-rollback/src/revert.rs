//! Applier trait — surface the monitor depends on without pulling the
//! gateway crate into this one. The concrete `impl Applier for
//! EvolutionApplier` lives in `corlinman-gateway::evolution_applier`
//! because gateway already depends on `corlinman-auto-rollback` (the
//! reverse dep would cycle).
//!
//! W1-B Step 4 will own `MonitorState::applier: Arc<dyn Applier>` so the
//! monitor never has to know which applier is wired in — production
//! plugs in `EvolutionApplier`, tests plug in a stubbed mock.

use async_trait::async_trait;
use corlinman_evolution::ProposalId;

/// Errors a revert can surface up to the monitor. Mirrors the four
/// failure modes the gateway's `EvolutionApplier::revert` distinguishes
/// (NotApplied / HistoryMissing / UnsupportedKind / Internal); the
/// gateway-side adapter maps its richer `ApplyError` into these.
#[derive(Debug, thiserror::Error)]
pub enum RevertError {
    /// Proposal id wasn't in `evolution_proposals`.
    #[error("proposal not found: {0}")]
    NotFound(String),
    /// Proposal exists but isn't in `applied` (already rolled back, or
    /// never made it to apply). Carries the actual status string.
    #[error("proposal not applied (status={0})")]
    NotApplied(String),
    /// History row missing — the forward apply must have written one,
    /// so this signals data corruption rather than a routine miss.
    #[error("history row missing for proposal {0}")]
    HistoryMissing(String),
    /// Kind has no revert handler yet (W1-B ships memory_op only).
    #[error("kind {0} cannot be reverted yet")]
    UnsupportedKind(String),
    /// Anything the gateway couldn't classify above (kb mutation
    /// failure, malformed inverse_diff, transaction error, ...). The
    /// monitor logs + skips; an operator inspects the gateway logs.
    #[error("revert failed: {0}")]
    Internal(String),
}

/// Thin contract the AutoRollback monitor calls into. One method on
/// purpose — the monitor only ever needs "revert this id, here's why".
#[async_trait]
pub trait Applier: Send + Sync {
    async fn revert(&self, id: &ProposalId, reason: &str) -> Result<(), RevertError>;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// Minimal mock — captures the (id, reason) pair the monitor passed
    /// in, returns whatever the test pre-loaded into `result`. Use this
    /// shape in Step 4's monitor tests too.
    struct MockApplier {
        calls: Mutex<Vec<(String, String)>>,
        result: Mutex<Result<(), RevertError>>,
    }

    impl MockApplier {
        fn ok() -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                result: Mutex::new(Ok(())),
            }
        }
        fn err(e: RevertError) -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                result: Mutex::new(Err(e)),
            }
        }
    }

    #[async_trait]
    impl Applier for MockApplier {
        async fn revert(&self, id: &ProposalId, reason: &str) -> Result<(), RevertError> {
            self.calls
                .lock()
                .unwrap()
                .push((id.0.clone(), reason.to_string()));
            // `result` is a Mutex<Result<…>>; clone via re-create so we
            // don't move the only copy out of the cell.
            match &*self.result.lock().unwrap() {
                Ok(()) => Ok(()),
                Err(RevertError::NotFound(s)) => Err(RevertError::NotFound(s.clone())),
                Err(RevertError::NotApplied(s)) => Err(RevertError::NotApplied(s.clone())),
                Err(RevertError::HistoryMissing(s)) => Err(RevertError::HistoryMissing(s.clone())),
                Err(RevertError::UnsupportedKind(s)) => {
                    Err(RevertError::UnsupportedKind(s.clone()))
                }
                Err(RevertError::Internal(s)) => Err(RevertError::Internal(s.clone())),
            }
        }
    }

    #[tokio::test]
    async fn applier_trait_records_call_and_returns_ok() {
        let m = MockApplier::ok();
        let pid = ProposalId::new("evol-mock-001");
        m.revert(&pid, "test reason").await.unwrap();
        let calls = m.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "evol-mock-001");
        assert_eq!(calls[0].1, "test reason");
    }

    #[tokio::test]
    async fn applier_trait_propagates_each_error_variant() {
        // Smoke each variant so the trait surface stays exhaustive —
        // adding a new variant breaks this test, which is the point.
        for err in [
            RevertError::NotFound("p".into()),
            RevertError::NotApplied("approved".into()),
            RevertError::HistoryMissing("p".into()),
            RevertError::UnsupportedKind("tag_rebalance".into()),
            RevertError::Internal("kb closed".into()),
        ] {
            let label = format!("{err:?}");
            let m = MockApplier::err(err);
            let pid = ProposalId::new("evol-mock-002");
            let got = m.revert(&pid, "r").await.unwrap_err();
            assert_eq!(format!("{got:?}"), label);
        }
    }
}
