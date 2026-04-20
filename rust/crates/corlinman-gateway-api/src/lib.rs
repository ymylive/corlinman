//! corlinman-gateway-api — shared trait surface for the gateway's chat pipeline.
//!
//! The gateway itself owns the real implementation (wires up the Python
//! reasoning loop, tool executor, etc). Other in-process crates — channels,
//! scheduler, admin jobs — need to trigger the same pipeline without
//! round-tripping through HTTP. Putting the trait in its own crate breaks the
//! dependency cycle that would otherwise form when the gateway depends on
//! channels (for `run_qq_channel`) and channels depends on the gateway (for
//! `ChatService`).
//!
//! Dependency topology after M5:
//!
//! ```text
//!   corlinman-core
//!         ▲
//!         │
//!   corlinman-gateway-api   (this crate — trait only)
//!         ▲
//!         ├── corlinman-gateway      (impl trait)
//!         └── corlinman-channels     (depend on trait)
//! ```
//!
//! This crate is I/O-free: only data types and an async trait.

use std::sync::Arc;

use async_trait::async_trait;
use bytes::Bytes;
use corlinman_core::{CorlinmanError, FailoverReason};
use futures::stream::BoxStream;
use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

// Re-export the binding type so downstream crates (channels, scheduler) can
// populate [`InternalChatRequest::binding`] without importing
// `corlinman-core::channel_binding` directly.
pub use corlinman_core::channel_binding::ChannelBinding;

/// Internal chat request submitted by a channel / scheduler / admin task.
///
/// A deliberately thin shape. Everything else (placeholders, provider config,
/// tools json) is owned by the gateway and merged in by the real `ChatService`
/// implementation before handing off to the Python reasoning loop.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InternalChatRequest {
    pub model: String,
    pub messages: Vec<Message>,
    /// Pre-derived session_key (see `corlinman_core::channel_binding`). Empty
    /// string is allowed — callers that don't have a binding (e.g. one-shot
    /// admin tests) can leave it blank and the implementation will synthesise
    /// an ephemeral key.
    pub session_key: String,
    pub stream: bool,
    pub max_tokens: Option<u32>,
    pub temperature: Option<f32>,
    /// Non-text inputs attached to the user turn (images, audio, files).
    /// Populated by channel adapters that parse multimodal segments; the HTTP
    /// REST surface currently leaves this empty. Providers that don't support
    /// a given kind are expected to skip + warn (see per-provider adapters).
    #[serde(default)]
    pub attachments: Vec<Attachment>,
    /// Transport-level conversation locus (channel / account / thread / sender)
    /// backfilled by channel adapters for audit, per-tool approval, and
    /// context-assembler scoping. The HTTP REST path leaves this `None`
    /// today — the gateway derives a synthetic binding on the fly when it
    /// needs one. Wired through to `proto::v1::ChatStart.binding` by
    /// `corlinman-gateway::services::chat_service::build_chat_start`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<ChannelBinding>,
}

/// Non-text payload attached to a chat turn. One message can carry multiple.
///
/// `url` and `bytes` are mutually complementary: channel adapters that receive
/// the payload as a remote URL (QQ's `image.url`, Telegram's `file_id` after
/// resolution) leave `bytes = None` so no download cost is paid on the hot
/// path. Callers that already have the bytes in hand (`scheduler`, admin
/// imports) set `bytes = Some(..)` and leave `url = None`. Providers that
/// need bytes (e.g. base64-only vendors) download on demand.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Attachment {
    pub kind: AttachmentKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bytes: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file_name: Option<String>,
}

/// Coarse-grained attachment category. Mirrored 1:1 by the proto enum
/// `corlinman.v1.AttachmentKind` so the channels → gateway → Python path
/// doesn't need a translation table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AttachmentKind {
    Image,
    Audio,
    Video,
    File,
}

/// A chat message as submitted to the internal pipeline. Mirrors the OpenAI
/// shape minus fields the internal caller never sets (function_call, name).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: Role,
    #[serde(default)]
    pub content: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

/// Usage figures surfaced to the internal caller on completion.
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
}

/// Streaming event yielded by [`ChatService::run`].
///
/// Mirrors the three things a channel adapter cares about: incremental text
/// tokens, tool invocations (informational — channels don't need to act on
/// them), and the terminal `Done` / `Error`.
#[derive(Debug, Clone)]
pub enum InternalChatEvent {
    /// A fragment of assistant-visible text. Concatenate across events to
    /// recover the full message body.
    TokenDelta(String),
    /// A tool invocation emitted by the reasoning loop. Forwarded so consumers
    /// can log / observe; the gateway itself handles execution.
    ToolCall {
        plugin: String,
        tool: String,
        args_json: Bytes,
    },
    /// Terminal sentinel. After this the stream ends.
    Done {
        finish_reason: String,
        usage: Option<Usage>,
    },
    /// Upstream failure. The stream ends after this event.
    Error(InternalChatError),
}

/// Clone-friendly error view so `InternalChatEvent` stays `Clone` (the real
/// `CorlinmanError` contains non-`Clone` variants such as `io::Error`).
#[derive(Debug, Clone)]
pub struct InternalChatError {
    pub reason: FailoverReason,
    pub message: String,
}

impl From<CorlinmanError> for InternalChatError {
    fn from(err: CorlinmanError) -> Self {
        match err {
            CorlinmanError::Upstream { reason, message } => Self { reason, message },
            other => Self {
                reason: FailoverReason::Unknown,
                message: other.to_string(),
            },
        }
    }
}

/// Boxed stream of internal chat events.
pub type ChatEventStream = BoxStream<'static, InternalChatEvent>;

/// Trait implemented by the gateway; consumed by channels / scheduler.
///
/// Implementations MUST:
/// - Honour `cancel` — drop upstream work when the token fires.
/// - Terminate the stream after emitting exactly one `Done` or `Error`.
/// - Be cheap to call — the caller holds an `Arc<dyn ChatService>` and may
///   invoke `run` once per inbound message.
#[async_trait]
pub trait ChatService: Send + Sync {
    async fn run(&self, req: InternalChatRequest, cancel: CancellationToken) -> ChatEventStream;
}

/// Convenience type alias for the handle shape callers typically hold.
pub type SharedChatService = Arc<dyn ChatService>;
