//! `corlinman-canvas` — Phase 4 W3 C3 Canvas Host renderer.
//!
//! Pure-function transform from producer-submitted Canvas frame payloads
//! (`present` opcode, see `phase4-w3-c3-design.md`) into Tidepool-styled
//! HTML fragments. The crate is *transport-free*: it never touches the
//! network, never reads config files, and exposes a single
//! [`Renderer::render`] entry point that the gateway, CLI, and future
//! static-export consumers all share.
//!
//! Five artifact kinds are pinned for C3:
//!
//! - `code`      → syntect-highlighted HTML — **iter 2 (live)**
//! - `table`     → markdown / CSV → `<table>` — **iter 3 (live)**
//! - `latex`     → katex-rs → HTML+MathML — **iter 4 (live)**
//! - `sparkline` → hand-rolled SVG — **iter 5 (live)**
//! - `mermaid`   → deno_core sandbox → SVG — **iter 6 (gated; see
//!   `adapters/mermaid.rs`)**
//!
//! Iter 1 landed only the protocol surface and a stub
//! [`Renderer`] that returned [`CanvasError::Unimplemented`]. Iter 2
//! wires the `code` adapter (syntect, class-based emission). Iter 3
//! wires `table`. Iter 4 wires `latex` (pure-Rust `katex-rs`). Iter 5
//! wires `sparkline` (hand-rolled SVG, no new dep). Iter 6 wires the
//! `mermaid` dispatch arm; the adapter is feature-gated behind
//! `--features mermaid` because the underlying `deno_core` (V8) dep
//! costs ~600 MB to the build. With the feature off (default) the
//! adapter returns a typed `CanvasError::Adapter` so the gateway and
//! UI render the structured fallback panel.
//!
//! Tidepool aesthetic enforcement: every rendered artifact carries a
//! `theme_class` (one of `"tp-light"` / `"tp-dark"`) plus class-only
//! HTML — no inline colours. CSS classes resolve to `--tp-*` design
//! tokens at the browser, so theme switches re-paint without re-render.

#![deny(missing_docs)]

pub mod adapters;
pub mod protocol;

pub use protocol::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload,
    RenderedArtifact, ThemeClass,
};

/// Renderer entry point. Dispatches on
/// [`CanvasPresentPayload::artifact_kind`] to the matching
/// [`adapters`] function. From iter 6 every kind in
/// [`ArtifactBody`] is wired to a real adapter — `Unimplemented` is
/// reserved for protocol-level surprises that should not occur in
/// runtime paths.
///
/// `Renderer::new()` is intentionally cheap — heavy state lives
/// behind `OnceLock`s in the adapters (e.g. the syntect
/// `SyntaxSet`, the deno_core `JsRuntime` under `--features
/// mermaid`). Cloning the `Renderer` is free.
#[derive(Debug, Default, Clone)]
pub struct Renderer {
    _private: (),
}

impl Renderer {
    /// Construct a fresh renderer. Cheap on every iter — heavy
    /// adapter state (syntect's `SyntaxSet`, katex-rs's
    /// `KatexContext`, the future deno_core `JsRuntime`) lives
    /// behind `OnceLock`s in the per-adapter modules.
    pub fn new() -> Self {
        Self { _private: () }
    }

    /// Render a `present`-frame payload to a single
    /// [`RenderedArtifact`]. Pure for code/table/latex/sparkline;
    /// mermaid (iter 6) introduces non-determinism for animation
    /// IDs only, output is otherwise stable.
    pub fn render(
        &self,
        payload: &CanvasPresentPayload,
    ) -> Result<RenderedArtifact, CanvasError> {
        let theme = payload.theme_hint.unwrap_or_default();
        match &payload.body {
            ArtifactBody::Code { language, source } => {
                adapters::code::render(language, source, theme)
            }
            ArtifactBody::Table { markdown, csv } => {
                adapters::table::render(
                    markdown.as_deref(),
                    csv.as_deref(),
                    theme,
                )
            }
            ArtifactBody::Latex { tex, display } => {
                adapters::latex::render(tex, *display, theme)
            }
            ArtifactBody::Sparkline { values, unit } => {
                adapters::sparkline::render(values, unit.as_deref(), theme)
            }
            ArtifactBody::Mermaid { diagram } => {
                adapters::mermaid::render(diagram, theme)
            }
        }
    }
}

