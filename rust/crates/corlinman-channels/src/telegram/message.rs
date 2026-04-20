//! Telegram Bot API wire types — the minimum corlinman decodes.
//!
//! Reference: <https://core.telegram.org/bots/api>.
//!
//! Only the fields the adapter uses are modelled; unknown fields are ignored
//! (serde default) so future Bot API revisions don't break the reader loop.
//!
//! Everything here is pure data — no I/O, no config.

use serde::{Deserialize, Serialize};

use corlinman_core::channel_binding::ChannelBinding;

/// One item from `getUpdates`. We only peel `update_id` + `message`; other
/// inbound variants (edited_message, channel_post, callback_query, ...) are
/// silently dropped for now — see TODOs in [`Update::message`].
#[derive(Debug, Clone, Deserialize)]
pub struct Update {
    pub update_id: i64,
    #[serde(default)]
    pub message: Option<Message>,
}

/// A Telegram [`Message`]. Fields outside our scope (photo/video/voice/
/// sticker/document/edit_date) are left off — they deserialise as ignored.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Message {
    pub message_id: i64,
    #[serde(default)]
    pub from: Option<User>,
    pub chat: Chat,
    /// Unix seconds.
    pub date: i64,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub entities: Vec<MessageEntity>,
    /// Present when this message is a reply to another. Used by the
    /// @-mention check to short-circuit when the user replied to a bot
    /// message (Telegram convention for "talking to the bot").
    #[serde(default)]
    pub reply_to_message: Option<Box<Message>>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct User {
    pub id: i64,
    #[serde(default)]
    pub is_bot: bool,
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub first_name: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Chat {
    pub id: i64,
    /// "private" | "group" | "supergroup" | "channel".
    #[serde(rename = "type")]
    pub chat_type: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub username: Option<String>,
}

impl Chat {
    /// Private 1:1 DM? Used to skip keyword/mention filtering.
    pub fn is_private(&self) -> bool {
        self.chat_type == "private"
    }
}

/// Subset of [`MessageEntity::type`] we need to detect mentions.
/// Unknown entity types deserialise into [`MessageEntity::Other`] and are
/// ignored by [`is_mentioning_bot`].
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum MessageEntity {
    /// `@username` mention — requires matching the username against the bot's
    /// own `@botname`.
    Mention {
        offset: i64,
        length: i64,
    },
    /// Mention of a user without `@username` (e.g. inline name link). Carries
    /// the target `user.id` so we can compare against the bot's id directly.
    TextMention {
        offset: i64,
        length: i64,
        user: User,
    },
    BotCommand {
        offset: i64,
        length: i64,
    },
    #[serde(other)]
    Other,
}

/// True iff the message contains a mention of the bot (by id or by username).
///
/// Both forms are considered:
/// - `TextMention { user.id == bot_id }` — the reliable path (works for every
///   bot, even one without a username).
/// - `Mention { offset, length }` spanning text equal to `@<bot_username>` —
///   bot usernames are unique on Telegram so substring equality is sufficient.
///
/// A reply-to-bot-message (bot's own prior message → user replied to it) is
/// treated as a mention by the caller, not here — that check belongs to the
/// service layer because it needs the bot's own id. See
/// [`crate::telegram::service`] for the composite check.
pub fn is_mentioning_bot(msg: &Message, bot_id: i64, bot_username: Option<&str>) -> bool {
    let Some(text) = msg.text.as_deref() else {
        return false;
    };
    for entity in &msg.entities {
        match entity {
            MessageEntity::TextMention { user, .. } if user.id == bot_id => return true,
            MessageEntity::Mention { offset, length } => {
                if let Some(bot_uname) = bot_username {
                    let slice = utf16_slice(text, *offset, *length);
                    // Telegram mentions include the leading '@'.
                    let expected = format!("@{bot_uname}");
                    if slice.eq_ignore_ascii_case(&expected) {
                        return true;
                    }
                }
            }
            _ => {}
        }
    }
    false
}

/// Telegram entity offsets/lengths are counted in UTF-16 code units (not
/// bytes, not chars). We re-encode to UTF-16 to slice precisely; this costs
/// an allocation but keeps the comparison correct for Chinese / emoji
/// messages where bytes != chars != utf16 units.
fn utf16_slice(text: &str, offset: i64, length: i64) -> String {
    let Ok(off) = usize::try_from(offset) else {
        return String::new();
    };
    let Ok(len) = usize::try_from(length) else {
        return String::new();
    };
    let units: Vec<u16> = text.encode_utf16().collect();
    let end = off.saturating_add(len).min(units.len());
    if off >= units.len() {
        return String::new();
    }
    String::from_utf16_lossy(&units[off..end])
}

/// Derive a [`ChannelBinding`] from a Telegram message.
///
/// Conventions (matches QQ adapter's shape):
/// - `channel = "telegram"`
/// - `account = bot_id` (the `self_id` equivalent)
/// - `thread = chat.id` for groups; for private chats the `chat.id` equals the
///   peer user id per Telegram's API, so `thread == sender` — matches
///   `ChannelBinding::qq_private`.
/// - `sender = message.from.id`; falls back to `chat.id` when `from` is absent
///   (anonymous channel posts).
pub fn binding_from_message(msg: &Message, bot_id: i64) -> ChannelBinding {
    let sender = msg.from.as_ref().map(|u| u.id).unwrap_or(msg.chat.id);
    ChannelBinding {
        channel: "telegram".to_string(),
        account: bot_id.to_string(),
        thread: msg.chat.id.to_string(),
        sender: sender.to_string(),
    }
}

// ============================================================================
// Outbound: sendMessage payload
// ============================================================================

/// Body for `POST /bot<token>/sendMessage`. Only the subset corlinman uses;
/// other fields (reply_markup, parse_mode, link_preview_options) are not sent.
#[derive(Debug, Clone, Serialize)]
pub struct SendMessageParams {
    pub chat_id: i64,
    pub text: String,
    /// When `Some`, reply to the given message id (Telegram treats this as
    /// `reply_parameters.message_id` in modern API, still accepts the legacy
    /// field for backwards compat).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to_message_id: Option<i64>,
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_group_message() -> Message {
        serde_json::from_value(serde_json::json!({
            "message_id": 42,
            "from": { "id": 555, "is_bot": false, "username": "alice" },
            "chat": { "id": -1001, "type": "supergroup", "title": "hangout" },
            "date": 1_700_000_000,
            "text": "@corlinman_bot hello there",
            "entities": [
                { "type": "mention", "offset": 0, "length": 14 }
            ]
        }))
        .unwrap()
    }

    #[test]
    fn parses_group_message_with_mention_entity() {
        let m = sample_group_message();
        assert_eq!(m.chat.id, -1001);
        assert_eq!(m.from.as_ref().unwrap().id, 555);
        assert_eq!(m.entities.len(), 1);
        assert!(matches!(m.entities[0], MessageEntity::Mention { .. }));
    }

    #[test]
    fn mention_username_matches_bot() {
        let m = sample_group_message();
        assert!(is_mentioning_bot(&m, 999, Some("corlinman_bot")));
        // Wrong username → not a match.
        assert!(!is_mentioning_bot(&m, 999, Some("someone_else")));
    }

    #[test]
    fn mention_text_mention_matches_by_user_id() {
        let raw = serde_json::json!({
            "message_id": 1,
            "from": { "id": 5, "is_bot": false },
            "chat": { "id": 10, "type": "group" },
            "date": 1,
            "text": "hi bot",
            "entities": [
                { "type": "text_mention", "offset": 3, "length": 3,
                  "user": { "id": 999, "is_bot": true } }
            ]
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        assert!(is_mentioning_bot(&m, 999, None));
        assert!(!is_mentioning_bot(&m, 1, None));
    }

    #[test]
    fn unknown_entity_type_does_not_fail_parse() {
        let raw = serde_json::json!({
            "message_id": 1,
            "chat": { "id": 10, "type": "private" },
            "date": 1,
            "text": "hello",
            "entities": [
                { "type": "hashtag", "offset": 0, "length": 5 }
            ]
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        assert_eq!(m.entities.len(), 1);
        assert!(matches!(m.entities[0], MessageEntity::Other));
    }

    #[test]
    fn event_to_channel_binding() {
        let m = sample_group_message();
        let b = binding_from_message(&m, 999);
        assert_eq!(b.channel, "telegram");
        assert_eq!(b.account, "999");
        assert_eq!(b.thread, "-1001");
        assert_eq!(b.sender, "555");
        // session_key is stable + 16 hex chars (verified by core).
        assert_eq!(b.session_key().len(), 16);
    }

    #[test]
    fn binding_private_chat_uses_chat_id_as_thread() {
        let raw = serde_json::json!({
            "message_id": 1,
            "from": { "id": 77, "is_bot": false },
            "chat": { "id": 77, "type": "private" },
            "date": 1,
            "text": "hi"
        });
        let m: Message = serde_json::from_value(raw).unwrap();
        let b = binding_from_message(&m, 999);
        assert_eq!(b.thread, "77");
        assert_eq!(b.sender, "77");
    }

    #[test]
    fn utf16_slice_handles_unicode_offsets() {
        // "你好 @bot": "你" and "好" are each 1 utf-16 unit; space is 1;
        // "@bot" is 4. So "@bot" starts at offset 3 with length 4.
        let s = "你好 @bot";
        let slice = utf16_slice(s, 3, 4);
        assert_eq!(slice, "@bot");
    }

    #[test]
    fn chat_is_private_only_for_private_type() {
        let private = Chat {
            id: 1,
            chat_type: "private".into(),
            title: None,
            username: None,
        };
        let group = Chat {
            id: 2,
            chat_type: "supergroup".into(),
            title: None,
            username: None,
        };
        assert!(private.is_private());
        assert!(!group.is_private());
    }
}
