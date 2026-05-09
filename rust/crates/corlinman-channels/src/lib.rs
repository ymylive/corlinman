//! corlinman-channels — inbound transports.
//!
//! Each submodule adapts an external transport (QQ/OneBot, LogStream WS,
//! Telegram HTTPS long-poll) into the internal `ChatRequest`; `router` derives
//! the `session_key` via `corlinman_core::channel_binding` so downstream RAG
//! / approval logic sees a transport-agnostic conversation locus.

pub mod channel;
pub mod logstream;
pub mod qq;
pub mod rate_limit;
pub mod router;
pub mod service;
pub mod telegram;

pub use channel::{
    spawn_all, ApnsChannel, Channel, ChannelContext, ChannelError, ChannelRegistry, QqChannel,
    TelegramChannel,
};
