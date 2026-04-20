//! corlinman-channels — inbound transports.
//!
//! Each submodule adapts an external transport (QQ/OneBot, LogStream WS) into
//! the internal `ChatRequest`; `router` derives the `session_key` via
//! `corlinman_core::channel_binding` so downstream RAG / approval logic sees
//! a transport-agnostic conversation locus.

pub mod logstream;
pub mod qq;
pub mod router;
