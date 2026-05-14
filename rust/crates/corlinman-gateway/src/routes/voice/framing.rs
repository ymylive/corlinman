//! Wire-format primitives for the `corlinman.voice.v1` subprotocol.
//!
//! Iter 2 of D4. Pure parsing helpers — no axum, no tokio, no I/O.
//! The upgrade handler in [`super::ws`] composes these into the live
//! WebSocket loop in iter 4+. Keeping the framing logic in a leaf
//! module also lets us cover the wire shape with focused unit tests
//! without spinning a full WebSocket harness.
//!
//! See `docs/design/phase4-w4-d4-design.md` §"Protocol surface".
//!
//! Wire layout:
//!
//! - **Subprotocol**: a single token, `corlinman.voice.v1`. Anything
//!   else is a hard-fail (close code 1002).
//! - **Audio frames**: WebSocket binary messages carrying raw
//!   little-endian PCM-16. Each frame must be at most ~200 ms; framing
//!   itself is just byte concatenation, so the parser only checks the
//!   length is a multiple of 2 (one PCM-16 sample = 2 bytes) and
//!   refuses pathological short / long frames.
//! - **Control frames**: WebSocket text messages carrying a JSON object
//!   with a discriminating `type` field. The set is fixed; unknown
//!   types are rejected so a misbehaving client can't smuggle future
//!   shapes through an old gateway.

use std::borrow::Cow;

use serde::{Deserialize, Serialize};

/// Canonical subprotocol token. Mounted in [`SUBPROTOCOLS`] for
/// [`accept_subprotocol`].
pub const SUBPROTOCOL: &str = "corlinman.voice.v1";

/// Subprotocol whitelist — exactly one entry today, but kept as a
/// constant slice so a future `corlinman.voice.v2` (e.g. Opus payloads)
/// can be added without touching the upgrade handler.
pub const SUBPROTOCOLS: &[&str] = &[SUBPROTOCOL];

/// Maximum binary audio frame size we accept from a client.
///
/// Design says "≤200 ms per frame". 16 kHz × 16-bit × 0.2 s = 6_400
/// bytes. We pad to 8_192 (8 KiB) to absorb 24 kHz client streams +
/// header padding from any future encapsulation; anything larger is
/// either a malformed client or an attempted DoS.
pub const MAX_AUDIO_FRAME_BYTES: usize = 8_192;

/// Smallest meaningful audio frame: one PCM-16 sample (2 bytes).
/// Smaller is a protocol error.
pub const MIN_AUDIO_FRAME_BYTES: usize = 2;

// ---------------------------------------------------------------------------
// Subprotocol negotiation
// ---------------------------------------------------------------------------

/// Outcome of a subprotocol negotiation. The upgrade handler maps
/// `Reject` to a 1002 close (or pre-upgrade 400) and `Accept` to
/// `Sec-WebSocket-Protocol: corlinman.voice.v1` on the upgrade response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubprotocolDecision {
    /// Client offered `corlinman.voice.v1`; reply with the same.
    Accept(&'static str),
    /// Client offered nothing or only unknown protocols. Reject with
    /// the supplied human-readable reason for telemetry / close-frame
    /// reason text.
    Reject(Cow<'static, str>),
}

/// Negotiate against the comma-separated value of the
/// `Sec-WebSocket-Protocol` request header.
///
/// `None` (header absent) is **rejected** — design contract says a
/// `/voice` upgrade without an explicit subprotocol is ambiguous and
/// must be refused so future v2 clients aren't silently downgraded.
///
/// Multiple protocols separated by `,` (the RFC 6455 shape) are
/// scanned in order; the first match wins.
pub fn accept_subprotocol(header: Option<&str>) -> SubprotocolDecision {
    let raw = match header {
        Some(s) if !s.trim().is_empty() => s,
        _ => {
            return SubprotocolDecision::Reject(Cow::Borrowed(
                "missing Sec-WebSocket-Protocol header; expected corlinman.voice.v1",
            ))
        }
    };
    for token in raw.split(',').map(|t| t.trim()).filter(|t| !t.is_empty()) {
        if SUBPROTOCOLS.contains(&token) {
            // Return the canonical reference rather than the user-supplied
            // slice so the upgrade response uses our spelling (case &
            // whitespace canonicalised).
            return SubprotocolDecision::Accept(SUBPROTOCOL);
        }
    }
    SubprotocolDecision::Reject(Cow::Owned(format!(
        "no supported subprotocol in offered set; offered=[{raw}], expected=[{SUBPROTOCOL}]"
    )))
}

// ---------------------------------------------------------------------------
// PCM-16 binary frame parsing
// ---------------------------------------------------------------------------

/// What the framing layer understood from a binary frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioFrame<'a> {
    /// Borrow of the raw little-endian PCM-16 bytes. The upgrade
    /// handler hands these to the provider adapter without
    /// per-sample copies.
    pub pcm_le_bytes: &'a [u8],
    /// Number of i16 samples = bytes / 2.
    pub sample_count: usize,
}

/// Reasons a binary frame is rejected. Mapped to a `close` frame on
/// repeated offences; the design's "drop if > 100 frames/sec" defence
/// piggybacks on this same path.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum AudioFrameError {
    #[error("audio frame is empty")]
    Empty,
    #[error("audio frame too small: got {got} bytes, minimum {min}")]
    TooSmall { got: usize, min: usize },
    #[error("audio frame too large: got {got} bytes, max {max}")]
    TooLarge { got: usize, max: usize },
    #[error("audio frame length must be even (PCM-16 = 2 bytes per sample); got {got}")]
    OddLength { got: usize },
}

/// Validate a binary frame and return a borrowed view of the PCM-16
/// payload. Pure: no allocation, no I/O.
pub fn parse_audio_frame(bytes: &[u8]) -> Result<AudioFrame<'_>, AudioFrameError> {
    if bytes.is_empty() {
        return Err(AudioFrameError::Empty);
    }
    if bytes.len() < MIN_AUDIO_FRAME_BYTES {
        return Err(AudioFrameError::TooSmall {
            got: bytes.len(),
            min: MIN_AUDIO_FRAME_BYTES,
        });
    }
    if bytes.len() > MAX_AUDIO_FRAME_BYTES {
        return Err(AudioFrameError::TooLarge {
            got: bytes.len(),
            max: MAX_AUDIO_FRAME_BYTES,
        });
    }
    if bytes.len() % 2 != 0 {
        return Err(AudioFrameError::OddLength { got: bytes.len() });
    }
    Ok(AudioFrame {
        pcm_le_bytes: bytes,
        sample_count: bytes.len() / 2,
    })
}

// ---------------------------------------------------------------------------
// Control frame JSON
// ---------------------------------------------------------------------------

/// Client → server control frame.
///
/// Tagged on the `"type"` field per the design's wire matrix. Adding a
/// new variant is intentionally a breaking change — old gateways must
/// reject new types until the upgrade handler explicitly maps them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ClientControl {
    /// First message after upgrade. Carries the chat session-key the
    /// transcript writes will hang under and the sample rate the
    /// client is producing.
    Start {
        session_key: String,
        #[serde(default)]
        agent_id: Option<String>,
        #[serde(default = "default_sample_rate_in")]
        sample_rate_hz: u32,
        #[serde(default = "default_format")]
        format: String,
    },
    /// Flushes server-side TTS buffer; provider barge-in. No payload.
    Interrupt,
    /// Operator decision relay (Mac-client hardware-shortcut path).
    /// Default flow goes through `/admin/approvals`.
    ApproveTool {
        approval_id: String,
        #[serde(default)]
        approve: bool,
    },
    /// Graceful close. Server flushes transcript + closes with 1000.
    End,
}

/// Server → client control frame.
///
/// Mirrors the design matrix. The same `serde(tag = "type")` scheme as
/// [`ClientControl`] keeps client-side parsers symmetric.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerControl {
    /// Ack of `start`. Includes the resolved session id (server-minted)
    /// and the provider alias actually selected.
    Started {
        session_id: String,
        provider: String,
    },
    /// Interim ASR transcript — partial, may change.
    TranscriptPartial { role: String, text: String },
    /// Final committed user/assistant turn.
    TranscriptFinal { role: String, text: String },
    /// Assistant turn text mirroring TTS audio.
    AgentText { text: String },
    /// Pause point — operator approval needed.
    ToolApprovalRequired {
        approval_id: String,
        tool: String,
        #[serde(default)]
        args: serde_json::Value,
    },
    /// Soft notice — minutes_remaining seconds before budget cap.
    BudgetWarning { minutes_remaining: u32 },
    /// Terminal — close frame follows.
    Error { code: String, message: String },
}

fn default_sample_rate_in() -> u32 {
    16_000
}
fn default_format() -> String {
    "pcm16".to_string()
}

/// Parse a text control frame. Returns the typed enum or a
/// human-readable error for telemetry.
pub fn parse_client_control(text: &str) -> Result<ClientControl, ControlParseError> {
    serde_json::from_str(text).map_err(|e| ControlParseError {
        message: e.to_string(),
    })
}

/// Serialise a server-side control event for an outbound text frame.
/// Infallible by construction (every variant's payload is JSON-safe);
/// returning a `String` rather than `Result<String, _>` keeps the call
/// sites that emit dozens of these per session noise-free.
pub fn encode_server_control(event: &ServerControl) -> String {
    serde_json::to_string(event).expect("ServerControl variants are always JSON-serialisable")
}

/// Parse error wrapper that doesn't leak the underlying serde_json
/// internals into upstream telemetry.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid control frame: {message}")]
pub struct ControlParseError {
    pub message: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    // ----- subprotocol negotiation -----

    #[test]
    fn subprotocol_accepts_canonical() {
        let d = accept_subprotocol(Some("corlinman.voice.v1"));
        assert_eq!(d, SubprotocolDecision::Accept(SUBPROTOCOL));
    }

    #[test]
    fn subprotocol_accepts_one_among_many() {
        // Browsers send a comma-separated list; first match wins.
        let d = accept_subprotocol(Some("foo, corlinman.voice.v1, bar"));
        assert_eq!(d, SubprotocolDecision::Accept(SUBPROTOCOL));
    }

    #[test]
    fn subprotocol_rejects_unknown() {
        let d = accept_subprotocol(Some("corlinman.voice.v0"));
        assert!(matches!(d, SubprotocolDecision::Reject(_)));
    }

    #[test]
    fn subprotocol_rejects_missing_header() {
        let d = accept_subprotocol(None);
        assert!(matches!(d, SubprotocolDecision::Reject(_)));
    }

    #[test]
    fn subprotocol_rejects_empty_header() {
        let d = accept_subprotocol(Some(""));
        assert!(matches!(d, SubprotocolDecision::Reject(_)));
    }

    // ----- PCM-16 binary frame parsing -----

    #[test]
    fn audio_frame_minimal_two_bytes_one_sample() {
        let f = parse_audio_frame(&[0x00, 0x01]).expect("two bytes is one PCM-16 sample");
        assert_eq!(f.sample_count, 1);
        assert_eq!(f.pcm_le_bytes.len(), 2);
    }

    #[test]
    fn audio_frame_typical_20ms_at_16khz() {
        // 20 ms at 16 kHz mono PCM-16 = 320 samples = 640 bytes.
        let buf = vec![0u8; 640];
        let f = parse_audio_frame(&buf).expect("typical 20ms frame parses");
        assert_eq!(f.sample_count, 320);
    }

    #[test]
    fn audio_frame_rejects_empty() {
        assert_eq!(parse_audio_frame(&[]), Err(AudioFrameError::Empty));
    }

    #[test]
    fn audio_frame_rejects_single_byte() {
        assert!(matches!(
            parse_audio_frame(&[0x42]),
            Err(AudioFrameError::TooSmall { got: 1, .. })
        ));
    }

    #[test]
    fn audio_frame_rejects_odd_length() {
        // Three bytes is even smaller than min in some contracts but the
        // contract here is "even count": min is satisfied (>=2), so we
        // expect the OddLength branch specifically.
        let buf = vec![0u8; 3];
        assert_eq!(
            parse_audio_frame(&buf),
            Err(AudioFrameError::OddLength { got: 3 })
        );
    }

    #[test]
    fn audio_frame_rejects_oversize() {
        let buf = vec![0u8; MAX_AUDIO_FRAME_BYTES + 2];
        assert!(matches!(
            parse_audio_frame(&buf),
            Err(AudioFrameError::TooLarge { .. })
        ));
    }

    // ----- Control frame JSON -----

    #[test]
    fn client_start_round_trips_with_defaults() {
        let json = r#"{"type":"start","session_key":"voice-abc"}"#;
        let parsed = parse_client_control(json).expect("start parses");
        match parsed {
            ClientControl::Start {
                session_key,
                agent_id,
                sample_rate_hz,
                format,
            } => {
                assert_eq!(session_key, "voice-abc");
                assert_eq!(agent_id, None);
                assert_eq!(sample_rate_hz, 16_000);
                assert_eq!(format, "pcm16");
            }
            other => panic!("expected Start, got {other:?}"),
        }
    }

    #[test]
    fn client_start_accepts_full_payload() {
        let json = r#"{
            "type":"start",
            "session_key":"k",
            "agent_id":"a-1",
            "sample_rate_hz":24000,
            "format":"pcm16"
        }"#;
        let parsed = parse_client_control(json).expect("full start parses");
        if let ClientControl::Start {
            session_key,
            agent_id,
            sample_rate_hz,
            format,
        } = parsed
        {
            assert_eq!(session_key, "k");
            assert_eq!(agent_id.as_deref(), Some("a-1"));
            assert_eq!(sample_rate_hz, 24_000);
            assert_eq!(format, "pcm16");
        } else {
            panic!("expected Start variant");
        }
    }

    #[test]
    fn client_interrupt_parses() {
        let parsed = parse_client_control(r#"{"type":"interrupt"}"#).expect("interrupt parses");
        assert_eq!(parsed, ClientControl::Interrupt);
    }

    #[test]
    fn client_approve_tool_parses() {
        let json = r#"{"type":"approve_tool","approval_id":"x-1","approve":true}"#;
        let parsed = parse_client_control(json).expect("approve_tool parses");
        assert_eq!(
            parsed,
            ClientControl::ApproveTool {
                approval_id: "x-1".into(),
                approve: true,
            }
        );
    }

    #[test]
    fn client_end_parses() {
        let parsed = parse_client_control(r#"{"type":"end"}"#).expect("end parses");
        assert_eq!(parsed, ClientControl::End);
    }

    #[test]
    fn client_unknown_type_rejected() {
        let err = parse_client_control(r#"{"type":"explode"}"#)
            .expect_err("unknown type must be rejected");
        assert!(err.message.to_lowercase().contains("explode") || !err.message.is_empty());
    }

    #[test]
    fn client_extra_field_on_struct_variant_rejected() {
        // deny_unknown_fields keeps malicious clients from smuggling
        // forward-compatible fields through an old gateway. (Serde's
        // internally-tagged + deny_unknown_fields doesn't reliably
        // catch extras on unit variants like `end`, so we exercise a
        // struct variant where the contract is firm.)
        let err = parse_client_control(
            r#"{"type":"approve_tool","approval_id":"x","approve":true,"extra":"oops"}"#,
        )
        .expect_err("unknown extra field on struct variant must be rejected");
        assert!(!err.message.is_empty());
    }

    #[test]
    fn server_started_round_trips() {
        let ev = ServerControl::Started {
            session_id: "voice-1".into(),
            provider: "openai-realtime".into(),
        };
        let s = encode_server_control(&ev);
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["type"], "started");
        assert_eq!(v["session_id"], "voice-1");
        assert_eq!(v["provider"], "openai-realtime");
    }

    #[test]
    fn server_transcript_partial_round_trips() {
        let s = encode_server_control(&ServerControl::TranscriptPartial {
            role: "user".into(),
            text: "hel".into(),
        });
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["type"], "transcript_partial");
        assert_eq!(v["role"], "user");
        assert_eq!(v["text"], "hel");
    }

    #[test]
    fn server_budget_warning_carries_minutes() {
        let s = encode_server_control(&ServerControl::BudgetWarning {
            minutes_remaining: 1,
        });
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["type"], "budget_warning");
        assert_eq!(v["minutes_remaining"], 1);
    }

    #[test]
    fn server_error_round_trips() {
        let s = encode_server_control(&ServerControl::Error {
            code: "budget_exhausted".into(),
            message: "cap reached".into(),
        });
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["type"], "error");
        assert_eq!(v["code"], "budget_exhausted");
    }
}
