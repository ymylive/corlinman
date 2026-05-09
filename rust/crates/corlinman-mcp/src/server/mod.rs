//! Server-side wiring for the MCP `/mcp` WebSocket endpoint.
//!
//! Iter 3 shipped the [`session`] state machine.
//! Iter 4 adds [`transport`]: the axum-mounted `/mcp` route, the
//! pre-upgrade auth gate, and the per-connection reader/writer loop.
//! Iter 5+ adds `dispatch`, `auth` (token ACL), and the capability
//! adapters under [`crate::adapters`].

pub mod auth;
pub mod session;
pub mod transport;

pub use auth::{resolve_token, TokenAcl, DEFAULT_TENANT_ID};
pub use session::{
    initialize_reply, SessionPhase, SessionState, INITIALIZED_NOTIFICATION,
    INITIALIZE_METHOD,
};
pub use transport::{
    FrameHandler, McpServer, McpServerConfig, StubMethodNotFoundHandler,
    CLOSE_CODE_MESSAGE_TOO_BIG, DEFAULT_MAX_FRAME_BYTES,
};
