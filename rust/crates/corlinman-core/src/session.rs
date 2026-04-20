//! `SessionStore` — cross-request conversation history persistence.
//!
//! The gateway threads a per-session history (user/assistant/tool messages)
//! between successive `POST /v1/chat/completions` calls so a follow-up turn
//! can reference prior content without the client resending it. Backends
//! implement [`SessionStore`]; the production impl is
//! [`crate::session_sqlite::SqliteSessionStore`].
//!
//! The store is intentionally simple: one ordered append-log per
//! `session_key`. `seq` is assigned server-side so two concurrent appends to
//! the same key can't collide.

use async_trait::async_trait;

use crate::CorlinmanError;

/// Role of a persisted message. Matches the OpenAI chat roles with a
/// dedicated `Tool` variant so tool responses round-trip cleanly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionRole {
    User,
    Assistant,
    System,
    Tool,
}

impl SessionRole {
    /// Wire representation used by the SQLite backend.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::System => "system",
            Self::Tool => "tool",
        }
    }

    /// Inverse of [`Self::as_str`]. Unknown strings fall back to `User` — the
    /// caller already validated the role at the request boundary.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Self {
        match s {
            "assistant" => Self::Assistant,
            "system" => Self::System,
            "tool" => Self::Tool,
            _ => Self::User,
        }
    }
}

/// One persisted message. `ts` is server-assigned at append time.
///
/// `tool_calls` carries the OpenAI-standard tool_calls array (stringified
/// JSON value) when an assistant message requested tool execution; `None`
/// otherwise. `tool_call_id` links a tool-role message back to the call it
/// answers.
#[derive(Debug, Clone)]
pub struct SessionMessage {
    pub role: SessionRole,
    pub content: String,
    pub tool_call_id: Option<String>,
    pub tool_calls: Option<serde_json::Value>,
    pub ts: time::OffsetDateTime,
}

impl SessionMessage {
    /// Convenience constructor for a user message with `ts = now()`.
    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: SessionRole::User,
            content: content.into(),
            tool_call_id: None,
            tool_calls: None,
            ts: time::OffsetDateTime::now_utc(),
        }
    }

    /// Convenience constructor for an assistant message with `ts = now()`.
    pub fn assistant(content: impl Into<String>, tool_calls: Option<serde_json::Value>) -> Self {
        Self {
            role: SessionRole::Assistant,
            content: content.into(),
            tool_call_id: None,
            tool_calls,
            ts: time::OffsetDateTime::now_utc(),
        }
    }
}

/// Trait implemented by any backing store (SQLite, memory, remote) capable of
/// persisting ordered message histories per session.
#[async_trait]
pub trait SessionStore: Send + Sync {
    /// Load full history for a session, ordered by `seq` ascending. Returns
    /// an empty vec when the session doesn't exist.
    async fn load(&self, session_key: &str) -> Result<Vec<SessionMessage>, CorlinmanError>;

    /// Append one message to the session. The store assigns the next `seq`.
    async fn append(
        &self,
        session_key: &str,
        message: SessionMessage,
    ) -> Result<(), CorlinmanError>;

    /// Delete every message for the session. No-op if the session doesn't
    /// exist.
    async fn delete(&self, session_key: &str) -> Result<(), CorlinmanError>;

    /// Keep only the `keep_last_n` most recent messages; delete older ones.
    /// `keep_last_n == 0` is equivalent to [`Self::delete`].
    async fn trim(&self, session_key: &str, keep_last_n: usize) -> Result<(), CorlinmanError>;
}
