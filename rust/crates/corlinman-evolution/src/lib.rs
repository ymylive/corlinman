//! corlinman EvolutionLoop — persistence contract.
//!
//! See `docs/design/auto-evolution.md` for the full architecture. This crate
//! owns three concerns:
//!
//! 1. **Types** — `EvolutionSignal`, `EvolutionProposal`, `EvolutionHistory`
//!    plus the `EvolutionKind` / `EvolutionRisk` / `EvolutionStatus` enums.
//! 2. **Schema** — the `SCHEMA_SQL` constant. A fresh `EvolutionStore::open()`
//!    applies it idempotently (`CREATE … IF NOT EXISTS`).
//! 3. **Repos** — async traits + SQLite implementations for signals /
//!    proposals / history. Phase 2 wave 1 agents (gateway observer, admin
//!    API, Python engine) all consume these traits to persist their state.
//!
//! The Python `EvolutionEngine` reads via raw SQL against the same SQLite
//! file (default `/data/evolution.sqlite`); the schema is the cross-language
//! contract.

pub mod repo;
pub mod schema;
pub mod store;
pub mod types;

pub use repo::{
    iso_week_window, ApplyIntent, EvolutionGuardConfig, HistoryRepo, IntentLogRepo, ProposalsRepo,
    RepoError, SignalsRepo,
};
pub use schema::SCHEMA_SQL;
pub use store::{EvolutionStore, OpenError};
pub use types::{
    meta, EvolutionHistory, EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionSignal,
    EvolutionStatus, ProposalId, ShadowMetrics, SignalSeverity,
};
