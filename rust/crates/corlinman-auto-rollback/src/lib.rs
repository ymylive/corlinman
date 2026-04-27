//! corlinman AutoRollback — Phase 3 Wave 1-B.
//!
//! Sits downstream of the EvolutionApplier. Once a proposal is `Applied`
//! (status terminal in W1-A's flow), this crate's monitor periodically
//! checks: did the metrics for the targets the proposal touched degrade
//! beyond a configurable threshold relative to the baseline snapshot
//! captured at apply time?
//!
//! When the answer is yes, the monitor fabricates a rollback proposal
//! (`kind = <original kind>`, `rollback_of = <original id>`,
//! `auto_rollback_at` + `auto_rollback_reason` set) and routes it
//! through the Applier's revert path — which replays the original
//! `inverse_diff` against the touched store and writes the
//! `rolled_back_at` / `rollback_reason` columns on the history row.
//!
//! ## Layout
//!
//! - [`metrics`] — signal-stream snapshot + delta computation. Builds
//!   on `evolution_signals` rather than scraping Prometheus so the
//!   monitor is fully self-contained inside the corlinman process tree
//!   (Step 2 fills this in).
//! - [`revert`]  — bridge to the Applier's reverse path: parse
//!   `inverse_diff`, dispatch per kind, write history.rolled_back_at
//!   (Step 3 fills this in).
//! - [`monitor`] — orchestration: list applied proposals in the grace
//!   window, compute deltas, decide-or-skip, drive revert (Step 4
//!   fills this in).
//!
//! W1-B Step 1 lands the crate skeleton, schema columns, and config
//! plumbing only; the three modules are intentional stubs the next
//! steps fill in — same pattern as W1-A's shadow-tester scaffold.

pub mod metrics;
pub mod monitor;
pub mod revert;

pub use monitor::{AutoRollbackMonitor, RunSummary};
pub use revert::{Applier, RevertError};
