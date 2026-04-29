//! corlinman ShadowTester — Phase 3 Wave 1-A.
//!
//! Sits between the Python `EvolutionEngine` (writes `pending` proposals)
//! and the operator approval queue. For medium/high-risk proposals it
//!
//! 1. Loads matching eval cases from `[evolution.shadow].eval_set_dir`
//!    (per-kind subdirs under that root).
//! 2. Runs each case in an in-process sandbox against a tempdir copy of
//!    `kb.sqlite` — production state is never written.
//! 3. Captures `shadow_metrics` (post-change) + `baseline_metrics_json`
//!    (pre-change) and an `eval_run_id` for traceability.
//! 4. Transitions the row `pending → shadow_running → shadow_done`, so
//!    the admin UI can render a measured delta before the operator
//!    decides.
//!
//! Low-risk kinds (Phase 2's `memory_op` is the only one shipping in
//! v0.3) skip ShadowTester entirely and remain on the original
//! `pending → approved` path.
//!
//! ## Layout
//!
//! - [`eval`]      — `EvalCase` / `EvalSet` types and YAML loader (Step 2).
//! - [`simulator`] — [`simulator::KindSimulator`] trait + per-kind impls (Step 3).
//! - [`runner`]    — [`runner::ShadowRunner`] orchestration (Step 3).
//!
//! Step 1 lands the crate skeleton + database/config plumbing only; the
//! three modules are intentional stubs the next steps fill in.

pub mod eval;
pub mod runner;
pub mod sandbox;
pub mod simulator;

pub use runner::{RunSummary, ShadowRunner};
pub use sandbox::{
    sha256_hex, DockerBackend, InProcessBackend, SandboxBackend, SandboxError, SelfTestResult,
};
pub use simulator::{KindSimulator, SimulatorError, SimulatorOutput};
