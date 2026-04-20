//! Channel dispatcher: `MessageEvent` → keyword filter → `ChatRequest`.
//!
//! The router is the first point inside corlinman that has an opinion about
//! *whether* to respond. It reads:
//! - `QQ_GROUP_KEYWORDS` (JSON map) to decide which group messages qualify,
//! - the bot's `self_id` list so `@mention` triggers bypass keyword filtering,
//!
//! and emits a transport-agnostic [`ChatRequest`] keyed by `session_key`.
//!
//! The full gateway chat pipeline lives in `corlinman-gateway`; for M5 we stop
//! at emitting the `ChatRequest` so this milestone is testable without spinning
//! up the whole stack.

use std::collections::HashMap;

use corlinman_core::channel_binding::ChannelBinding;
use corlinman_core::types::ChatRequest;

use crate::qq::message::{is_mentioned, segments_to_text, MessageEvent, MessageType};

/// JSON schema for `QQ_GROUP_KEYWORDS`:
///
/// ```json
/// { "123456": ["格兰", "虎子"], "789012": ["爱弥斯"] }
/// ```
///
/// Group ids are stringified because JSON object keys must be strings; values
/// are case-insensitive substring matches against the flattened message text.
///
/// Groups absent from the map default to "dispatch every message" (matches
/// qqBot.js behaviour when `groupKeywordsMap[gid]` is undefined and the bot
/// has no global keyword list configured).
pub type GroupKeywords = HashMap<String, Vec<String>>;

/// Parse `QQ_GROUP_KEYWORDS` env var (JSON). Missing / empty env returns an
/// empty map — dispatch-all for every group.
pub fn parse_group_keywords(raw: &str) -> Result<GroupKeywords, serde_json::Error> {
    if raw.trim().is_empty() {
        return Ok(GroupKeywords::new());
    }
    serde_json::from_str(raw)
}

/// Router state. Cheap to clone (keeps an `Arc` internally once we wire a real
/// config store). For now it just owns the keyword map so tests can construct
/// one in-process.
#[derive(Debug, Default, Clone)]
pub struct ChannelRouter {
    /// Per-group keyword filter.
    pub group_keywords: GroupKeywords,
    /// `@mention` targets that always trigger, independent of keywords. In
    /// OneBot this is the bot's own `self_id`, but we leave it as a `Vec` in
    /// case multiple bot accounts share one gateway.
    pub self_ids: Vec<i64>,
}

impl ChannelRouter {
    pub fn new(group_keywords: GroupKeywords, self_ids: Vec<i64>) -> Self {
        Self {
            group_keywords,
            self_ids,
        }
    }

    /// Apply the keyword/mention gate and return a [`ChatRequest`] if the
    /// message should be forwarded to the chat pipeline.
    ///
    /// Returns `None` when the message is filtered out (heartbeat, wrong
    /// message_type, keyword mismatch, empty body, ...). All drops are silent
    /// — callers log at `debug` if they want visibility.
    pub fn dispatch(&self, event: &MessageEvent) -> Option<ChatRequest> {
        let text = flatten_and_trim(&event.message, &event.raw_message);

        // @mention short-circuits keyword filtering. Matches qqBot.js line 298-336.
        let mentioned = self
            .self_ids
            .iter()
            .any(|sid| is_mentioned(&event.message, *sid));

        let binding = match event.message_type {
            MessageType::Private => ChannelBinding::qq_private(event.self_id, event.user_id),
            MessageType::Group => {
                let group_id = event.group_id?;
                if !mentioned && !self.keyword_match(group_id, &text) {
                    return None;
                }
                ChannelBinding::qq_group(event.self_id, group_id, event.user_id)
            }
        };

        // Drop completely empty messages (pure sticker / pure recall placeholder).
        if text.trim().is_empty() {
            return None;
        }

        let mut req = ChatRequest::new(binding, text);
        req.message_id = Some(event.message_id.to_string());
        req.timestamp = event.time;
        req.mentioned = mentioned;
        Some(req)
    }

    fn keyword_match(&self, group_id: i64, text: &str) -> bool {
        let Some(kws) = self.group_keywords.get(&group_id.to_string()) else {
            // No keyword list configured → dispatch-all (default).
            return true;
        };
        if kws.is_empty() {
            return true;
        }
        let lower = text.to_lowercase();
        kws.iter().any(|kw| lower.contains(&kw.to_lowercase()))
    }
}

/// Prefer the OneBot-supplied `raw_message` (already CQ-flattened) when
/// present; otherwise fall back to re-extracting from segments.
fn flatten_and_trim(segs: &[crate::qq::message::MessageSegment], raw: &str) -> String {
    if !raw.is_empty() {
        raw.to_string()
    } else {
        segments_to_text(segs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::qq::message::{MessageSegment, Sender};

    fn group_event(raw: &str, segs: Vec<MessageSegment>, gid: i64) -> MessageEvent {
        MessageEvent {
            self_id: 100,
            message_type: MessageType::Group,
            sub_type: Some("normal".into()),
            group_id: Some(gid),
            user_id: 200,
            message_id: 1,
            message: segs,
            raw_message: raw.into(),
            time: 1700000000,
            sender: Some(Sender::default()),
        }
    }

    #[test]
    fn dispatch_all_when_group_absent_from_map() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = group_event("随便聊聊", vec![MessageSegment::text("随便聊聊")], 9999);
        let req = router.dispatch(&ev).unwrap();
        assert_eq!(req.content, "随便聊聊");
        assert_eq!(req.binding.thread, "9999");
    }

    #[test]
    fn keyword_match_is_case_insensitive() {
        let mut map = GroupKeywords::new();
        map.insert("123".into(), vec!["格兰".into(), "Aemeath".into()]);
        let router = ChannelRouter::new(map, vec![100]);

        let ev = group_event("hey AEMEATH are you there", vec![], 123);
        assert!(router.dispatch(&ev).is_some());

        let ev2 = group_event("irrelevant chatter", vec![], 123);
        assert!(router.dispatch(&ev2).is_none());
    }

    #[test]
    fn mention_bypasses_keyword_filter() {
        let mut map = GroupKeywords::new();
        map.insert("123".into(), vec!["never_matches".into()]);
        let router = ChannelRouter::new(map, vec![100]);

        let ev = group_event(
            "[CQ:at,qq=100] help",
            vec![MessageSegment::at("100"), MessageSegment::text(" help")],
            123,
        );
        let req = router.dispatch(&ev).unwrap();
        assert!(req.mentioned);
    }

    #[test]
    fn private_message_always_dispatches() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = MessageEvent {
            self_id: 100,
            message_type: MessageType::Private,
            sub_type: None,
            group_id: None,
            user_id: 77,
            message_id: 1,
            message: vec![MessageSegment::text("hi")],
            raw_message: "hi".into(),
            time: 1,
            sender: None,
        };
        let req = router.dispatch(&ev).unwrap();
        assert_eq!(req.binding.channel, "qq");
        assert_eq!(req.binding.thread, "77");
    }

    #[test]
    fn empty_group_message_drops() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev = group_event("", vec![], 123);
        assert!(router.dispatch(&ev).is_none());
    }

    #[test]
    fn parse_keywords_env_json() {
        let raw = r#"{"123":["a","b"],"456":["c"]}"#;
        let m = parse_group_keywords(raw).unwrap();
        assert_eq!(m.get("123").unwrap().len(), 2);
    }

    #[test]
    fn parse_empty_env_returns_empty_map() {
        assert!(parse_group_keywords("").unwrap().is_empty());
        assert!(parse_group_keywords("   ").unwrap().is_empty());
    }

    #[test]
    fn session_key_stable_across_events() {
        let router = ChannelRouter::new(GroupKeywords::new(), vec![100]);
        let ev1 = group_event("一号消息", vec![MessageSegment::text("一号消息")], 321);
        let ev2 = group_event("二号消息", vec![MessageSegment::text("二号消息")], 321);
        let r1 = router.dispatch(&ev1).unwrap();
        let r2 = router.dispatch(&ev2).unwrap();
        assert_eq!(r1.session_key, r2.session_key);
    }
}
