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

use std::sync::Arc;

pub mod adapters;
pub mod cache;
pub mod protocol;

pub use cache::{key_for as cache_key_for, key_to_hex, CacheKey, RenderCache, RENDERER_VERSION};
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
///
/// Iter 7 adds an embedded [`RenderCache`]. The default constructor
/// keeps the cache disabled so existing tests stay byte-identical;
/// the gateway's iter-8 wiring uses [`Renderer::with_cache`] to
/// honour the operator-tunable `[canvas] cache_max_entries`.
#[derive(Debug, Default, Clone)]
pub struct Renderer {
    cache: RenderCache,
}

impl Renderer {
    /// Construct a fresh renderer with caching disabled. Cheap on
    /// every iter — heavy adapter state (syntect's `SyntaxSet`,
    /// katex-rs's `KatexContext`, the future deno_core `JsRuntime`)
    /// lives behind `OnceLock`s in the per-adapter modules.
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct a renderer backed by a fixed-capacity LRU. Capacity
    /// `0` disables the cache entirely (every `render` re-dispatches
    /// to its adapter); the gateway exposes this knob via
    /// `[canvas] cache_max_entries`.
    pub fn with_cache(capacity: usize) -> Self {
        Self {
            cache: RenderCache::new(capacity),
        }
    }

    /// Borrow the embedded cache (for stats / admin endpoints).
    pub fn cache(&self) -> &RenderCache {
        &self.cache
    }

    /// Render a `present`-frame payload to a single
    /// [`RenderedArtifact`]. Pure for code/table/latex/sparkline;
    /// mermaid (iter 6) introduces non-determinism for animation
    /// IDs only, output is otherwise stable.
    ///
    /// Iter 7 wraps the dispatch with cache lookup / insertion. On
    /// cache hit the call is `O(1)`; on miss the adapter runs, the
    /// `content_hash` is populated from the cache key, and the
    /// result is inserted before being returned to the caller. The
    /// `Arc<RenderedArtifact>` form is cheap-clone on subsequent
    /// hits.
    pub fn render(
        &self,
        payload: &CanvasPresentPayload,
    ) -> Result<RenderedArtifact, CanvasError> {
        let theme = payload.theme_hint.unwrap_or_default();
        let key = cache::key_for(payload.artifact_kind, &payload.body, theme);
        if let Some(hit) = self.cache.get(&key) {
            // `Arc<RenderedArtifact>` → owned clone for the public
            // wire shape. Cheap: the inner `String`s already exist.
            return Ok((*hit).clone());
        }

        let mut artifact = match &payload.body {
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
        }?;

        // Stamp the content hash so clients can dedup network
        // responses without re-hashing the HTML fragment.
        if artifact.content_hash.is_empty() {
            artifact.content_hash = cache::key_to_hex(&key);
        }

        if !self.cache.is_disabled() {
            let arc = Arc::new(artifact.clone());
            self.cache.insert(key, arc);
        }
        Ok(artifact)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload(source: &str) -> CanvasPresentPayload {
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Code,
            body: ArtifactBody::Code {
                language: "rust".into(),
                source: source.into(),
            },
            idempotency_key: "art_t".into(),
            theme_hint: Some(ThemeClass::TpLight),
        }
    }

    #[test]
    fn render_populates_content_hash() {
        let r = Renderer::new();
        let out = r.render(&payload("fn main() {}")).unwrap();
        assert_eq!(out.content_hash.len(), 64);
        assert!(out.content_hash.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn equal_inputs_produce_equal_content_hash() {
        let r = Renderer::new();
        let a = r.render(&payload("fn main() {}")).unwrap();
        let b = r.render(&payload("fn main() {}")).unwrap();
        assert_eq!(a.content_hash, b.content_hash);
    }

    #[test]
    fn different_sources_produce_different_content_hash() {
        let r = Renderer::new();
        let a = r.render(&payload("fn main() {}")).unwrap();
        let b = r.render(&payload("fn other() {}")).unwrap();
        assert_ne!(a.content_hash, b.content_hash);
    }

    #[test]
    fn cache_hit_short_circuits_adapter() {
        let r = Renderer::with_cache(8);
        let p = payload("fn main() {}");
        let _ = r.render(&p).unwrap();
        assert_eq!(r.cache().len(), 1);
        let _ = r.render(&p).unwrap();
        // Second call must not insert again.
        assert_eq!(r.cache().len(), 1);
    }

    #[test]
    fn disabled_cache_does_not_grow() {
        let r = Renderer::new();
        let _ = r.render(&payload("fn main() {}")).unwrap();
        let _ = r.render(&payload("fn other() {}")).unwrap();
        assert_eq!(r.cache().len(), 0);
        assert!(r.cache().is_disabled());
    }

    #[test]
    fn cache_evicts_at_capacity_via_render() {
        let r = Renderer::with_cache(2);
        let _ = r.render(&payload("a")).unwrap();
        let _ = r.render(&payload("b")).unwrap();
        let _ = r.render(&payload("c")).unwrap();
        assert_eq!(r.cache().len(), 2);
    }
}

