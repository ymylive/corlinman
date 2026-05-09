//! Canvas Host protocol types.
//!
//! These shapes are the C3 contract between producer (skill / tool /
//! engine) and the renderer. They sit *inside* the Phase-1 `present`
//! frame's `payload` field — the gateway frame whitelist
//! (`canvas.rs:48`) does not grow in C3. See
//! `phase4-w3-c3-design.md` § "Protocol surface".
//!
//! Wire shape on `POST /canvas/frame`:
//!
//! ```json
//! {
//!   "session_id": "cs_…",
//!   "kind": "present",
//!   "payload": {
//!     "artifact_kind": "code",
//!     "body": { "language": "rust", "source": "fn main(){}" },
//!     "idempotency_key": "art_a1b2…",
//!     "theme_hint": "tp-dark"
//!   }
//! }
//! ```
//!
//! The renderer dispatches on `artifact_kind`; `body` is an untagged
//! enum so producers send the shape native to each kind without
//! Serde adjacent-tag overhead. Unknown `artifact_kind` deserialises
//! to a typed [`CanvasError::UnknownKind`] at the gateway boundary,
//! not a 500.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Closed enum of artifact kinds the C3 renderer understands. Adding
/// a new kind requires touching this enum *and* the renderer
/// dispatch — intentional: closed vocabulary, no producer surprises.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactKind {
    /// Syntax-highlighted source code via syntect.
    Code,
    /// Mermaid diagram rendered via embedded deno_core to inline SVG.
    Mermaid,
    /// GFM markdown or CSV table → `<table>`.
    Table,
    /// TeX → MathML/HTML via katex-rs.
    Latex,
    /// Inline SVG sparkline from a numeric series.
    Sparkline,
}

impl ArtifactKind {
    /// Wire-name as it appears in `payload.artifact_kind`.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Code => "code",
            Self::Mermaid => "mermaid",
            Self::Table => "table",
            Self::Latex => "latex",
            Self::Sparkline => "sparkline",
        }
    }
}

/// Producer-supplied body for one artifact. Untagged enum: the
/// `artifact_kind` field on [`CanvasPresentPayload`] is the
/// discriminator, so each variant carries only its own native fields
/// (no adjacent-tag noise on the wire).
///
/// Field requirements per kind are documented inline; producers that
/// send an unexpected shape get a serde error at the gateway, not at
/// the renderer.
/// `ArtifactBody` is *not* a plain `Serialize`/`Deserialize` enum:
/// the design wants an "untagged" wire form (no Serde-level tag inside
/// `body`) but with the outer `artifact_kind` as the discriminator.
///
/// Naïve `#[serde(untagged)]` is unsafe because variant `Table` has
/// only optional fields and would silently match any other variant's
/// payload (regression caught by iter 1's
/// `protocol_present_payload_round_trips`). And serde does not allow
/// `#[serde(deny_unknown_fields)]` on untagged variants.
///
/// Instead, [`CanvasPresentPayload`] has a custom `Deserialize` impl
/// (see below) that reads `artifact_kind` first, then dispatches to
/// per-variant struct deserialisers — giving us tagged-style safety
/// with untagged-style wire ergonomics.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum ArtifactBody {
    /// `code` body. `language` is a syntect-recognised name (rust,
    /// python, ts, …); unknown languages fall back to a plain `<pre>`
    /// in the renderer (no error). `source` is the raw program text.
    Code {
        /// Source language hint (e.g. `"rust"`, `"python"`).
        language: String,
        /// Raw source text — HTML-escaped by the renderer.
        source: String,
    },
    /// `mermaid` body. The diagram source is parsed by the bundled
    /// `mermaid.min.js` inside the deno_core sandbox.
    Mermaid {
        /// Mermaid source (`graph LR; A-->B`).
        diagram: String,
    },
    /// `latex` body.
    Latex {
        /// TeX source.
        tex: String,
        /// `true` → block / `katex-display`; `false` → inline span.
        #[serde(default)]
        display: bool,
    },
    /// `sparkline` body.
    Sparkline {
        /// Numeric series — minimum 2 points, capped at 1024
        /// server-side.
        values: Vec<f64>,
        /// Optional unit label (rendered as `<title>` for a11y).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        unit: Option<String>,
    },
    /// `table` body. Exactly one of `markdown` / `csv` — the renderer
    /// deserialises both fields as `Option<String>` and rejects if
    /// neither / both are present (validation in iter 3, table
    /// adapter).
    Table {
        /// GFM markdown table source.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        markdown: Option<String>,
        /// CSV table source.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        csv: Option<String>,
    },
}

// Per-variant deserialisation helpers. Each is a struct mirroring
// one `ArtifactBody` variant with `deny_unknown_fields` so the
// outer dispatch can reject malformed shapes precisely. Kept private
// — only the [`CanvasPresentPayload`] custom `Deserialize` calls
// these.

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CodeBody {
    language: String,
    source: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MermaidBody {
    diagram: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LatexBody {
    tex: String,
    #[serde(default)]
    display: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SparklineBody {
    values: Vec<f64>,
    #[serde(default)]
    unit: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TableBody {
    #[serde(default)]
    markdown: Option<String>,
    #[serde(default)]
    csv: Option<String>,
}

/// Top-level shape inside the `present` frame's `payload`.
///
/// Custom [`serde::Deserialize`] impl: reads `artifact_kind` first,
/// then dispatches `body` deserialisation to the matching per-variant
/// struct. Producers that send a body that doesn't match the declared
/// kind get a precise error like `unknown field \`tex\`` rather than
/// silent variant-misclassification.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CanvasPresentPayload {
    /// Discriminator for [`ArtifactBody`].
    pub artifact_kind: ArtifactKind,
    /// Shape-specific producer payload, validated against
    /// `artifact_kind` at deserialise time.
    pub body: ArtifactBody,
    /// Producer-chosen idempotency key. Two `present` frames with the
    /// same `(session_id, idempotency_key)` are deduplicated by the
    /// gateway; the renderer is invoked at most once.
    pub idempotency_key: String,
    /// Optional theme hint. The renderer always emits class-only
    /// HTML, but mermaid post-processing varies stroke colour by
    /// theme so the cache key includes this field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub theme_hint: Option<ThemeClass>,
}

impl<'de> Deserialize<'de> for CanvasPresentPayload {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        // Two-pass: parse to a raw bag, then re-parse `body` as the
        // typed variant matching `artifact_kind`. Cheap (the body is
        // already JSON-in-memory) and gives precise errors.
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Raw {
            artifact_kind: ArtifactKind,
            body: serde_json::Value,
            idempotency_key: String,
            #[serde(default)]
            theme_hint: Option<ThemeClass>,
        }

        let raw = Raw::deserialize(deserializer)?;

        let body = match raw.artifact_kind {
            ArtifactKind::Code => {
                let CodeBody { language, source } =
                    serde_json::from_value(raw.body).map_err(serde::de::Error::custom)?;
                ArtifactBody::Code { language, source }
            }
            ArtifactKind::Mermaid => {
                let MermaidBody { diagram } =
                    serde_json::from_value(raw.body).map_err(serde::de::Error::custom)?;
                ArtifactBody::Mermaid { diagram }
            }
            ArtifactKind::Latex => {
                let LatexBody { tex, display } =
                    serde_json::from_value(raw.body).map_err(serde::de::Error::custom)?;
                ArtifactBody::Latex { tex, display }
            }
            ArtifactKind::Sparkline => {
                let SparklineBody { values, unit } =
                    serde_json::from_value(raw.body).map_err(serde::de::Error::custom)?;
                ArtifactBody::Sparkline { values, unit }
            }
            ArtifactKind::Table => {
                let TableBody { markdown, csv } =
                    serde_json::from_value(raw.body).map_err(serde::de::Error::custom)?;
                ArtifactBody::Table { markdown, csv }
            }
        };

        Ok(CanvasPresentPayload {
            artifact_kind: raw.artifact_kind,
            body,
            idempotency_key: raw.idempotency_key,
            theme_hint: raw.theme_hint,
        })
    }
}

impl CanvasPresentPayload {
    /// Convenience: discriminator extraction.
    pub fn artifact_kind(&self) -> ArtifactKind {
        self.artifact_kind
    }
}

/// Theme tag for non-CSS-var consumers (Swift / mobile, future
/// static export). Web admins resolve `--tp-*` tokens directly and
/// can ignore this field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ThemeClass {
    /// Daytime / light surface.
    TpLight,
    /// Nighttime / dark surface.
    TpDark,
}

impl Default for ThemeClass {
    fn default() -> Self {
        Self::TpLight
    }
}

/// Renderer output. Self-contained: callers need only the fragment
/// + theme class to surface the artifact. Hash and warnings are
/// optional UX/diagnostics extras.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RenderedArtifact {
    /// Sanitised HTML fragment ready to drop inside a transcript
    /// container. Always class-only — no inline `style="color:…"`.
    pub html_fragment: String,
    /// Theme tag the producer asked for (or [`ThemeClass::TpLight`]
    /// by default).
    pub theme_class: ThemeClass,
    /// Stable hash of the rendered output bytes. Useful for client
    /// dedup / cache validation. Iter 1 emits empty string.
    #[serde(default)]
    pub content_hash: String,
    /// Echoes the artifact kind for client-side dispatch without
    /// re-parsing `html_fragment`.
    pub render_kind: ArtifactKind,
    /// Non-fatal renderer notes (e.g. `"language not recognised,
    /// fell back to plain"`). Surfaced as a small footer in the
    /// admin UI.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub warnings: Vec<String>,
}

/// Renderer-level error.
///
/// Each variant maps to a HTTP / SSE-event surface in the gateway:
/// `Unimplemented` and `UnknownKind` → `400`; `Timeout` /
/// `BodyTooLarge` → emit a `canvas-artifact-error` SSE payload so
/// the UI can show the dashed-glass fallback panel.
#[derive(Debug, Error)]
pub enum CanvasError {
    /// The kind is recognised but no adapter is wired in this
    /// iteration / build. Iter 1 returns this for *every* kind.
    #[error("renderer for `{kind:?}` not implemented in this build")]
    Unimplemented {
        /// Kind that was requested.
        kind: ArtifactKind,
    },
    /// Wire payload had an `artifact_kind` the renderer doesn't know
    /// about. Surfaces from serde at the gateway boundary; the
    /// renderer itself only handles the closed [`ArtifactKind`] enum.
    #[error("unknown canvas artifact kind: `{0}`")]
    UnknownKind(String),
    /// Producer body exceeded `[canvas] max_artifact_bytes`. Wired in
    /// iter 8 (gateway) and iter 6 (mermaid SVG cap).
    #[error("artifact body exceeded {max_bytes} bytes (kind={kind:?})")]
    BodyTooLarge {
        /// Configured ceiling.
        max_bytes: usize,
        /// Kind being rendered.
        kind: ArtifactKind,
    },
    /// Mermaid render exceeded `[canvas] render_timeout_ms`. Iter 6.
    #[error("renderer timed out after {timeout_ms} ms (kind={kind:?})")]
    Timeout {
        /// Configured ceiling.
        timeout_ms: u64,
        /// Kind being rendered.
        kind: ArtifactKind,
    },
    /// Adapter-specific parse / runtime failure. Carries a free-form
    /// message; the gateway surfaces it as a `canvas-artifact-error`
    /// payload.
    #[error("canvas adapter error ({kind:?}): {message}")]
    Adapter {
        /// Kind being rendered.
        kind: ArtifactKind,
        /// Human-readable adapter-specific reason.
        message: String,
    },
}
