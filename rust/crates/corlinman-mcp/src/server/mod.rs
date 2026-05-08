//! Server-side wiring for the MCP `/mcp` WebSocket endpoint.
//!
//! Iter 3 ships the [`session`] state machine only; later iters add
//! `transport`, `dispatch`, and `auth` siblings here. Keeping the
//! module tree pinned now means iter 4+ doesn't need to refactor
//! re-exports.

pub mod session;

pub use session::{
    initialize_reply, SessionPhase, SessionState, INITIALIZED_NOTIFICATION,
    INITIALIZE_METHOD,
};
