//! corlinman-subagent — Rust supervisor for the parent reasoning loop's
//! `subagent.spawn` tool.
//!
//! ## Why a Rust crate?
//!
//! The **isolation contract** (depth cap, concurrency slots, time
//! budget) needs to live somewhere the LLM cannot reach via prompt
//! injection. Pure-Python puts those caps in process memory the model
//! itself is steering; pure-Rust would have to duplicate the agent
//! servicer's persona / provider wiring. The split: Rust owns the
//! caps + lifecycle; Python (`corlinman_agent.subagent.runner`) owns
//! the actual `ReasoningLoop` driver. Rust calls back into Python via
//! PyO3 once the budget checks pass — see the `python` cargo feature.
//!
//! ## Iteration scope
//!
//! - **iter 1** (this crate's first commit): types only —
//!   [`TaskSpec`], [`TaskResult`], [`ParentContext`], [`FinishReason`]
//!   with serde + tests. No supervisor, no PyO3 bridge.
//! - **iter 2**: [`SubagentSupervisor`] with depth / concurrency /
//!   timeout primitives + slot drop-guard. Still no spawning.
//! - **iter 3**: PyO3 scaffold so a Python module can be re-entered
//!   from inside the supervisor under the `python` feature.
//!
//! The full 10-iter plan lives in `docs/design/phase4-w4-d3-design.md`.

pub mod types;

pub use types::{FinishReason, ParentContext, TaskResult, TaskSpec, ToolCallSummary};
