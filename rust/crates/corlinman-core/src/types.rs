//! Miscellaneous newtypes shared across crates (SessionKey, RequestId, Model)
//! plus the transport-agnostic `ChatRequest` a channel adapter emits into the
//! router.
//!
//! Keep this module I/O-free. All fields use owned `String` for now — we can
//! tighten to `&'static str` / `Arc<str>` later once the hot path is measured.

use serde::{Deserialize, Serialize};

use crate::channel_binding::ChannelBinding;

/// Chat turn handed from a channel adapter to the router / chat pipeline.
///
/// This is the M5 stub — enough to verify end-to-end QQ decoding without
/// committing to the final gateway pipeline shape. Fields likely to grow:
/// attachments (images/files), quoted message id, admin flag.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatRequest {
    /// Derived from `binding.session_key()` up front so downstream consumers
    /// don't need to hash again.
    pub session_key: String,
    /// Where this turn came from (channel / account / thread / sender).
    pub binding: ChannelBinding,
    /// Flattened user text, with CQ segments already collapsed. Empty-string
    /// allowed for pure-image messages (router decides what to do).
    pub content: String,
    /// Original message id from the transport (OneBot's `message_id` as a string),
    /// so the adapter can `reply` when sending back.
    pub message_id: Option<String>,
    /// Unix seconds; adapter-supplied. Used for rate-limit / dedup windows.
    pub timestamp: i64,
    /// Whether the bot was directly addressed (QQ @mention / Telegram reply-to).
    /// The router uses this to decide whether keyword filters still apply.
    pub mentioned: bool,
}

impl ChatRequest {
    pub fn new(binding: ChannelBinding, content: String) -> Self {
        let session_key = binding.session_key();
        Self {
            session_key,
            binding,
            content,
            message_id: None,
            timestamp: 0,
            mentioned: false,
        }
    }
}
