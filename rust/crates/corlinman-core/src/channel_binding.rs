//! `ChannelBinding → session_key` derivation, stable across transports.
//!
//! A `ChannelBinding` identifies the conversation locus for an inbound message:
//! `(channel, account, thread, sender)`. RAG, DailyNote, approval and chat
//! history all key off the resulting `session_key` so switching from QQ group
//! to Telegram DM gives a fresh context while re-entering the same QQ group
//! resumes the previous conversation.
//!
//! The hash is the first 16 hex chars of `sha256("<channel>|<account>|<thread>|<sender>")`.
//! 64 bits of entropy is plenty for disambiguating active chats and keeps the
//! key short enough to fit in log lines / file names without truncation.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// A transport-agnostic conversation locus.
///
/// Field conventions:
/// - `channel`: lowercase transport name (`"qq"`, `"telegram"`, `"discord"`, `"logstream"`).
/// - `account`: the bot's own id on that transport (self_id for OneBot). String
///   so non-numeric ids (discord snowflakes, telegram usernames) fit.
/// - `thread`: group id for group chats, peer user id for 1:1. For logstream-style
///   broadcasts use a stable topic name.
/// - `sender`: the user who sent the message. Equals `thread` for 1:1 QQ DMs.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ChannelBinding {
    pub channel: String,
    pub account: String,
    pub thread: String,
    pub sender: String,
}

impl ChannelBinding {
    /// Compute the stable 16-hex-char session key for this binding.
    ///
    /// Uses sha256 so the hash is collision-resistant and consistent across
    /// runs / machines. Only the first 64 bits are kept — ample for the
    /// active-conversations cardinality corlinman targets.
    pub fn session_key(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.channel.as_bytes());
        hasher.update(b"|");
        hasher.update(self.account.as_bytes());
        hasher.update(b"|");
        hasher.update(self.thread.as_bytes());
        hasher.update(b"|");
        hasher.update(self.sender.as_bytes());
        let digest = hasher.finalize();
        // First 8 bytes → 16 hex chars.
        let mut out = String::with_capacity(16);
        for b in &digest[..8] {
            use std::fmt::Write;
            let _ = write!(out, "{b:02x}");
        }
        out
    }

    /// Convenience constructor for OneBot v11 group messages.
    pub fn qq_group(self_id: i64, group_id: i64, sender: i64) -> Self {
        Self {
            channel: "qq".to_string(),
            account: self_id.to_string(),
            thread: group_id.to_string(),
            sender: sender.to_string(),
        }
    }

    /// Convenience constructor for OneBot v11 private messages.
    pub fn qq_private(self_id: i64, sender: i64) -> Self {
        Self {
            channel: "qq".to_string(),
            account: self_id.to_string(),
            // Private chats: thread == sender (the peer).
            thread: sender.to_string(),
            sender: sender.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_key_is_16_hex_chars() {
        let b = ChannelBinding::qq_group(100, 200, 300);
        let k = b.session_key();
        assert_eq!(k.len(), 16);
        assert!(k.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn session_key_is_stable() {
        let b1 = ChannelBinding::qq_group(100, 200, 300);
        let b2 = ChannelBinding::qq_group(100, 200, 300);
        assert_eq!(b1.session_key(), b2.session_key());
    }

    #[test]
    fn different_threads_produce_different_keys() {
        let g1 = ChannelBinding::qq_group(1, 2, 3).session_key();
        let g2 = ChannelBinding::qq_group(1, 9, 3).session_key();
        assert_ne!(g1, g2);
    }

    #[test]
    fn qq_private_uses_sender_as_thread() {
        let b = ChannelBinding::qq_private(10, 42);
        assert_eq!(b.thread, "42");
        assert_eq!(b.sender, "42");
        assert_eq!(b.channel, "qq");
    }

    #[test]
    fn channel_separation() {
        let qq = ChannelBinding {
            channel: "qq".into(),
            account: "1".into(),
            thread: "2".into(),
            sender: "3".into(),
        };
        let tg = ChannelBinding {
            channel: "telegram".into(),
            account: "1".into(),
            thread: "2".into(),
            sender: "3".into(),
        };
        assert_ne!(qq.session_key(), tg.session_key());
    }
}
