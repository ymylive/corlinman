//! corlinman-core — shared types, errors, parsers, and primitives.
//!
//! This crate is **pure library, no I/O**. Every other `corlinman-*` crate may
//! depend on it; it may not depend on any of them. See plan §2.
//!
//! Modules are organised so a downstream crate can `use corlinman_core::{
//! CorlinmanError, FailoverReason, backoff, cancel, …}` without dragging in
//! unrelated parsers.

pub mod backoff;
pub mod cancel;
pub mod channel_binding;
pub mod config;
pub mod error;
pub mod manifest;
pub mod metrics;
pub mod placeholder;
pub mod placeholders;
pub mod session;
pub mod session_sqlite;
pub mod types;

// Re-exports that are load-bearing for downstream crates.
pub use error::{CorlinmanError, FailoverReason};
pub use placeholder::{
    DynamicResolver, PlaceholderCtx, PlaceholderEngine, PlaceholderError, RenderContext,
    RESERVED_NAMESPACES,
};
pub use placeholders::{
    DiaryNamespaceResolver, FixedTime, NamespaceResolver, PlaceholderContext,
    PlaceholderContextBuilder, RagRetriever, RenderError, Renderer, SystemTime, TimeSource,
};
pub use session::{SessionMessage, SessionRole, SessionStore, SessionSummary};
pub use session_sqlite::SqliteSessionStore;
