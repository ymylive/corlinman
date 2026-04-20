//! OneBot v11 event + action types (serde-deserialisable).
//!
//! This is a **narrow** port of the wire shapes corlinman actually uses, not a
//! full OneBot SDK. We accept unknown `post_type` / `action` via untagged
//! fall-through in the top-level enums so an unexpected meta event doesn't kill
//! the connection.
//!
//! References:
//! - OneBot v11 spec: https://github.com/botuniverse/onebot-11

use corlinman_gateway_api::{Attachment, AttachmentKind};
use serde::{Deserialize, Serialize};

// ============================================================================
// Incoming events (gocq/NapCat → corlinman)
// ============================================================================

/// Top-level OneBot event. Tagged on `post_type`.
///
/// Unknown `post_type`s deserialize into [`Event::Unknown`] — keeps the reader
/// loop alive in the face of spec drift.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "post_type", rename_all = "snake_case")]
pub enum Event {
    Message(MessageEvent),
    Notice(NoticeEvent),
    #[serde(rename = "meta_event")]
    MetaEvent(MetaEvent),
    Request(RequestEvent),
    #[serde(other)]
    Unknown,
}

/// Group / private message event.
///
/// `message_type` is flattened via an internal tag so the same struct covers
/// both — matches qqBot.js's shared handler.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MessageEvent {
    pub self_id: i64,
    pub message_type: MessageType,
    #[serde(default)]
    pub sub_type: Option<String>,
    #[serde(default)]
    pub group_id: Option<i64>,
    pub user_id: i64,
    pub message_id: i64,
    pub message: Vec<MessageSegment>,
    #[serde(default)]
    pub raw_message: String,
    pub time: i64,
    #[serde(default)]
    pub sender: Option<Sender>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MessageType {
    Private,
    Group,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct Sender {
    #[serde(default)]
    pub user_id: Option<i64>,
    #[serde(default)]
    pub nickname: Option<String>,
    #[serde(default)]
    pub card: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
}

/// Notice events (group admin changes, recalls, friend add) — we don't act on
/// these yet but parse them so they don't look like protocol errors.
#[derive(Debug, Clone, Deserialize)]
pub struct NoticeEvent {
    pub self_id: i64,
    pub notice_type: String,
    pub time: i64,
    #[serde(default)]
    pub group_id: Option<i64>,
    #[serde(default)]
    pub user_id: Option<i64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MetaEvent {
    pub self_id: i64,
    pub meta_event_type: String,
    pub time: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RequestEvent {
    pub self_id: i64,
    pub request_type: String,
    pub time: i64,
    #[serde(default)]
    pub user_id: Option<i64>,
    #[serde(default)]
    pub group_id: Option<i64>,
    #[serde(default)]
    pub flag: Option<String>,
}

// ============================================================================
// Message segments (CQ codes, array form)
// ============================================================================

/// OneBot v11 CQ segment. Serialises as `{"type": "text", "data": {...}}` per
/// spec.
///
/// We parse the 7 segment types listed in the plan:
/// text / at / image / reply / face / record / forward. Anything else falls
/// through the untagged [`MessageSegment::Other`] variant (carries the raw
/// JSON so we can log the shape without failing the whole event).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum MessageSegment {
    Known(KnownSegment),
    Other(serde_json::Value),
}

/// Sub-enum for the seven segment types we understand. Kept separate so the
/// main enum can have an `Other(serde_json::Value)` fallback without breaking
/// serde's tagged-enum rules (only unit variants may use `#[serde(other)]`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", content = "data", rename_all = "snake_case")]
pub enum KnownSegment {
    Text {
        text: String,
    },
    At {
        qq: String,
    },
    Image {
        #[serde(default)]
        url: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        file: Option<String>,
    },
    Reply {
        id: String,
    },
    Face {
        id: String,
    },
    Record {
        #[serde(default)]
        url: String,
    },
    /// OneBot v11 "forward" (merged forward reference) inbound shape.
    Forward {
        id: String,
    },
}

impl MessageSegment {
    pub fn text<S: Into<String>>(s: S) -> Self {
        Self::Known(KnownSegment::Text { text: s.into() })
    }
    pub fn reply<S: Into<String>>(id: S) -> Self {
        Self::Known(KnownSegment::Reply { id: id.into() })
    }
    pub fn at<S: Into<String>>(qq: S) -> Self {
        Self::Known(KnownSegment::At { qq: qq.into() })
    }
    pub fn image<S: Into<String>>(url: S) -> Self {
        Self::Known(KnownSegment::Image {
            url: url.into(),
            file: None,
        })
    }
    pub fn face<S: Into<String>>(id: S) -> Self {
        Self::Known(KnownSegment::Face { id: id.into() })
    }

    /// Borrow the inner `KnownSegment` if this is a known type.
    pub fn known(&self) -> Option<&KnownSegment> {
        match self {
            Self::Known(k) => Some(k),
            Self::Other(_) => None,
        }
    }
}

// ============================================================================
// Outbound actions (corlinman → gocq/NapCat)
// ============================================================================

/// Outbound OneBot action. Serialises as `{"action": "...", "params": {...}}`.
///
/// Only the three action types corlinman emits today; the forward-node variant
/// covers the A-股分析 合并转发 path.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "action", content = "params", rename_all = "snake_case")]
pub enum Action {
    SendPrivateMsg {
        user_id: i64,
        message: Vec<MessageSegment>,
    },
    SendGroupMsg {
        group_id: i64,
        message: Vec<MessageSegment>,
    },
    SendGroupForwardMsg {
        group_id: i64,
        messages: Vec<ForwardNode>,
    },
}

/// Merged-forward node (OneBot v11 `node` segment).
#[derive(Debug, Clone, Serialize)]
pub struct ForwardNode {
    #[serde(rename = "type")]
    pub ty: &'static str, // always "node"
    pub data: ForwardNodeData,
}

#[derive(Debug, Clone, Serialize)]
pub struct ForwardNodeData {
    pub name: String,
    pub uin: String,
    pub content: Vec<MessageSegment>,
}

impl ForwardNode {
    pub fn new(name: impl Into<String>, uin: i64, content: Vec<MessageSegment>) -> Self {
        Self {
            ty: "node",
            data: ForwardNodeData {
                name: name.into(),
                uin: uin.to_string(),
                content,
            },
        }
    }
}

/// Flatten CQ segments into a single plain-text string, mirroring qqBot.js's
/// `_extractText`. `at` segments are kept as `@<qq>` tokens because the router
/// keyword match operates on the raw text.
pub fn segments_to_text(segs: &[MessageSegment]) -> String {
    let mut out = String::new();
    for seg in segs {
        match seg.known() {
            Some(KnownSegment::Text { text }) => out.push_str(text),
            Some(KnownSegment::At { qq }) => {
                out.push('@');
                out.push_str(qq);
                out.push(' ');
            }
            _ => {}
        }
    }
    out
}

/// Extract attachments (image/voice) from a QQ segment list.
///
/// OneBot's `image` and `record` segments carry a remote `url` (gocq/NapCat
/// pre-upload to their CDN). We pass the URL through — providers that accept
/// URL-form inputs (Anthropic Claude 4 image blocks, OpenAI vision) use it
/// directly; providers that need bytes download on demand. QQ has no `file`
/// segment in the current `KnownSegment` set, so this helper handles the two
/// multimodal variants corlinman actually decodes today. Other kinds
/// (video, generic file) can be added here when their segment variants land.
pub fn segments_to_attachments(segments: &[MessageSegment]) -> Vec<Attachment> {
    segments
        .iter()
        .filter_map(|seg| match seg.known()? {
            KnownSegment::Image { url, file } if !url.is_empty() => Some(Attachment {
                kind: AttachmentKind::Image,
                url: Some(url.clone()),
                bytes: None,
                // OneBot doesn't tell us the concrete mime; "image/*" signals
                // the provider to infer from the URL path or content.
                mime: Some("image/*".into()),
                file_name: file.clone(),
            }),
            KnownSegment::Record { url } if !url.is_empty() => Some(Attachment {
                kind: AttachmentKind::Audio,
                url: Some(url.clone()),
                bytes: None,
                mime: Some("audio/*".into()),
                file_name: None,
            }),
            _ => None,
        })
        .collect()
}

/// Whether any `at` segment targets `self_id` (or is `@all`).
pub fn is_mentioned(segs: &[MessageSegment], self_id: i64) -> bool {
    let self_s = self_id.to_string();
    segs.iter().any(
        |seg| matches!(seg.known(), Some(KnownSegment::At { qq }) if *qq == self_s || qq == "all"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_group_message_event() {
        let raw = serde_json::json!({
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": 1700000000,
            "self_id": 100,
            "user_id": 200,
            "group_id": 300,
            "message_id": 1,
            "message": [
                { "type": "at", "data": { "qq": "100" } },
                { "type": "text", "data": { "text": "hello" } }
            ],
            "raw_message": "[CQ:at,qq=100] hello",
            "sender": { "user_id": 200, "nickname": "alice" }
        });
        let ev: Event = serde_json::from_value(raw).unwrap();
        let Event::Message(m) = ev else {
            panic!("expected Message");
        };
        assert_eq!(m.group_id, Some(300));
        assert_eq!(m.message.len(), 2);
        assert!(is_mentioned(&m.message, 100));
    }

    #[test]
    fn parse_heartbeat_as_meta_event() {
        let raw = serde_json::json!({
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": 1700000000,
            "self_id": 100,
            "interval": 5000,
            "status": {}
        });
        let ev: Event = serde_json::from_value(raw).unwrap();
        assert!(matches!(ev, Event::MetaEvent(_)));
    }

    #[test]
    fn unknown_post_type_maps_to_unknown() {
        let raw = serde_json::json!({ "post_type": "mystery", "time": 0, "self_id": 0 });
        let ev: Event = serde_json::from_value(raw).unwrap();
        assert!(matches!(ev, Event::Unknown));
    }

    #[test]
    fn parse_all_seven_segment_types() {
        let inputs = [
            (r#"{"type":"text","data":{"text":"hi"}}"#, true),
            (r#"{"type":"at","data":{"qq":"1"}}"#, true),
            (
                r#"{"type":"image","data":{"url":"https://x","file":"f"}}"#,
                true,
            ),
            (r#"{"type":"reply","data":{"id":"42"}}"#, true),
            (r#"{"type":"face","data":{"id":"1"}}"#, true),
            (r#"{"type":"record","data":{"url":"https://y"}}"#, true),
            (r#"{"type":"forward","data":{"id":"fwd1"}}"#, true),
        ];
        for (json, ok) in inputs {
            let seg: Result<MessageSegment, _> = serde_json::from_str(json);
            assert_eq!(seg.is_ok(), ok, "failed for {json}");
        }
    }

    #[test]
    fn unknown_segment_maps_to_other() {
        let seg: MessageSegment =
            serde_json::from_str(r#"{"type":"video","data":{"url":"x"}}"#).unwrap();
        assert!(matches!(seg, MessageSegment::Other(_)));
        assert!(seg.known().is_none());
    }

    #[test]
    fn action_serializes_to_onebot_envelope() {
        let a = Action::SendGroupMsg {
            group_id: 1,
            message: vec![MessageSegment::reply("42"), MessageSegment::text("hello")],
        };
        let s = serde_json::to_value(&a).unwrap();
        assert_eq!(s["action"], "send_group_msg");
        assert_eq!(s["params"]["group_id"], 1);
        assert_eq!(s["params"]["message"][0]["type"], "reply");
        assert_eq!(s["params"]["message"][0]["data"]["id"], "42");
        assert_eq!(s["params"]["message"][1]["type"], "text");
    }

    #[test]
    fn segments_to_attachments_covers_image_and_record() {
        let segs = vec![
            MessageSegment::text("caption"),
            MessageSegment::Known(KnownSegment::Image {
                url: "https://cdn/img.jpg".into(),
                file: Some("img.jpg".into()),
            }),
            MessageSegment::Known(KnownSegment::Record {
                url: "https://cdn/voice.amr".into(),
            }),
            // Unknown segment (e.g. `video`) is passed through unchanged.
            MessageSegment::Other(serde_json::json!({"type": "video"})),
            // Face / at / reply don't produce attachments.
            MessageSegment::at("100"),
            MessageSegment::face("1"),
            MessageSegment::reply("42"),
        ];
        let atts = segments_to_attachments(&segs);
        assert_eq!(atts.len(), 2);

        let image = &atts[0];
        assert_eq!(image.kind, AttachmentKind::Image);
        assert_eq!(image.url.as_deref(), Some("https://cdn/img.jpg"));
        assert_eq!(image.file_name.as_deref(), Some("img.jpg"));
        assert!(image.bytes.is_none());

        let audio = &atts[1];
        assert_eq!(audio.kind, AttachmentKind::Audio);
        assert_eq!(audio.url.as_deref(), Some("https://cdn/voice.amr"));
    }

    #[test]
    fn segments_to_attachments_skips_empty_urls() {
        let segs = vec![MessageSegment::Known(KnownSegment::Image {
            url: String::new(),
            file: None,
        })];
        assert!(segments_to_attachments(&segs).is_empty());
    }

    #[test]
    fn segments_to_attachments_empty_when_text_only() {
        let segs = vec![MessageSegment::text("hi"), MessageSegment::at("100")];
        assert!(segments_to_attachments(&segs).is_empty());
    }

    #[test]
    fn text_extraction_flattens_segments() {
        let segs = vec![
            MessageSegment::at("100"),
            MessageSegment::text("hello "),
            MessageSegment::text("world"),
            MessageSegment::face("1"),
        ];
        let t = segments_to_text(&segs);
        assert!(t.contains("hello world"));
        assert!(t.contains("@100"));
    }
}
