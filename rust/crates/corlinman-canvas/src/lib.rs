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
//! - `mermaid`   → deno_core sandbox → SVG — iter 6
//!
//! Iter 1 landed only the protocol surface and a stub
//! [`Renderer`] that returned [`CanvasError::Unimplemented`]. Iter 2
//! wires the `code` adapter (syntect, class-based emission). Iter 3
//! wires `table`. Iter 4 wires `latex` (pure-Rust `katex-rs`). Iter 5
//! wires `sparkline` (hand-rolled SVG, no new dep). The mermaid
//! adapter remains `Unimplemented` until iter 6.
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
/// [`adapters`] function. Adapters that aren't wired yet return
/// [`CanvasError::Unimplemented`].
///
/// `Renderer::new()` is intentionally cheap — heavy state lives
/// behind `OnceLock`s in the adapters (e.g. the syntect
/// `SyntaxSet`). Cloning the `Renderer` is free.
#[derive(Debug, Default, Clone)]
pub struct Renderer {
    _private: (),
}

impl Renderer {
    /// Construct a fresh renderer. Cheap in iter 1-2; iter 6 will
    /// lazy-init the deno_core engine on first mermaid render.
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
            other_kind => Err(CanvasError::Unimplemented {
                kind: artifact_body_kind(other_kind),
            }),
        }
    }
}

/// Maps an [`ArtifactBody`] back to its [`ArtifactKind`] discriminator.
/// Cheap, branch-free in optimised builds.
fn artifact_body_kind(body: &ArtifactBody) -> ArtifactKind {
    match body {
        ArtifactBody::Code { .. } => ArtifactKind::Code,
        ArtifactBody::Mermaid { .. } => ArtifactKind::Mermaid,
        ArtifactBody::Table { .. } => ArtifactKind::Table,
        ArtifactBody::Latex { .. } => ArtifactKind::Latex,
        ArtifactBody::Sparkline { .. } => ArtifactKind::Sparkline,
    }
}
