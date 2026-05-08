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
//! - `code`      → syntect-highlighted HTML (iter 2)
//! - `table`     → markdown / CSV → `<table>` (iter 3)
//! - `latex`     → katex-rs → MathML (iter 4)
//! - `sparkline` → hand-rolled SVG (iter 5)
//! - `mermaid`   → deno_core sandbox → SVG (iter 6)
//!
//! Iter 1 (this commit) lands only the protocol surface and a stub
//! [`Renderer`] that returns [`CanvasError::Unimplemented`]. No
//! adapters, no caching, no config plumbing. Callers can already
//! depend on the wire types — the gateway wires this crate in iter 8.
//!
//! Tidepool aesthetic enforcement: every rendered artifact carries a
//! `theme_class` (one of `"tp-light"` / `"tp-dark"`) plus class-only
//! HTML — no inline colours. CSS classes resolve to `--tp-*` design
//! tokens at the browser, so theme switches re-paint without re-render.

#![deny(missing_docs)]

pub mod protocol;

pub use protocol::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload,
    RenderedArtifact, ThemeClass,
};

/// Renderer entry point. Iter 1 is a stub: every kind returns
/// [`CanvasError::Unimplemented`]. Subsequent iterations attach real
/// adapters (syntect, pulldown-cmark, katex, deno_core, sparkline).
///
/// `Renderer::new()` is intentionally cheap in this iteration — it
/// holds no state. Iter 2 introduces a syntect `SyntaxSet`/`ThemeSet`
/// loaded once at construction; iter 6 lazy-inits the deno_core
/// engine.
#[derive(Debug, Default, Clone)]
pub struct Renderer {
    _private: (),
}

impl Renderer {
    /// Construct a fresh renderer. Cheap in iter 1; later iterations
    /// build syntect / katex / deno_core state here.
    pub fn new() -> Self {
        Self { _private: () }
    }

    /// Render a `present`-frame payload to a single
    /// [`RenderedArtifact`]. Pure: same input, same output (modulo
    /// the deno_core path in iter 6 which adds non-determinism only
    /// for animation; mermaid output is otherwise stable).
    ///
    /// Iter 1 always returns [`CanvasError::Unimplemented`]. The
    /// signature is the contract callers (gateway, CLI, tests) bind
    /// to from now on.
    pub fn render(
        &self,
        payload: &CanvasPresentPayload,
    ) -> Result<RenderedArtifact, CanvasError> {
        let kind = payload.artifact_kind();
        Err(CanvasError::Unimplemented { kind })
    }
}
