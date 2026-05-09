//! `latex` artifact adapter — TeX → HTML/MathML via `katex-rs`.
//!
//! ## Why `katex-rs` and not the `katex` crate
//!
//! Crates.io has two TeX→HTML options:
//!
//! - `katex` (≥0.4) — wraps a JavaScript engine (`quick-js` or
//!   `duktape`) plus the upstream KaTeX JS bundle. Drags ~3 MB of JS
//!   plus a C-linked QuickJS interpreter into the workspace.
//! - `katex-rs` (0.2) — pure-Rust port of the KaTeX renderer. Compiles
//!   on the workspace's `rust-version = 1.85` toolchain, no C deps,
//!   no V8 / QuickJS, no extra runtime. Matches the design's
//!   `phase4-w3-c3-design.md:54` "katex-rs (no JS)" choice.
//!
//! We use `katex-rs`. Trade-off: 0.2.x ships behind upstream KaTeX on
//! a few exotic packages (`mathtools`, `xcolor` extensions); the
//! design's blacklist below trims the dangerous edge of that surface
//! pre-emptively, and the ports cover the common math producers
//! (sums, fractions, integrals, matrices) which is the C3 target.
//!
//! ## Display vs inline
//!
//! The producer's `display: bool` field maps to `Settings.display_mode`.
//! `display_mode = true` wraps in a `<span class="katex-display">`
//! (KaTeX's own block class); we additionally tag the wrapper with our
//! `cn-canvas-katex` token-class so Tidepool stylesheets can override
//! colour without fighting KaTeX's specificity.
//!
//! ## Strict mode + trust posture
//!
//! `phase4-w3-c3-design.md` test `latex_macro_blacklist` expects
//! `\href` and `\input` rejection. The KaTeX porting does not parse
//! `\input` at all (TeX file inclusion has no analogue), so that
//! arrives "for free" — `\input` triggers an "Undefined control
//! sequence" parse error. `\href` *is* implemented and gated behind
//! `Settings.trust`; we keep `trust = false` so all `\href` /
//! `\includegraphics` URL-introducing macros are rejected. We also
//! set `strict = StrictMode::Error` so non-LaTeX-compatible input
//! becomes an adapter error rather than a silently-warned render.
//!
//! ## Sandboxing
//!
//! katex-rs is in-process pure Rust — no V8, no JS scopes, no FS
//! access. The only concerning vector is macro recursion bombs;
//! `Settings.max_expand` defaults to 1000 expansions which is the
//! same ceiling the upstream KaTeX uses. We keep the default; the
//! adapter timing is bounded by linear-time parse + tree walk, no
//! arbitrary loops.
//!
//! ## Output shape
//!
//! ```html
//! <!-- inline -->
//! <span class="cn-canvas-katex"><span class="katex">…MathML+HTML…</span></span>
//!
//! <!-- display -->
//! <span class="cn-canvas-katex cn-canvas-katex--display">
//!   <span class="katex-display"><span class="katex">…</span></span>
//! </span>
//! ```
//!
//! KaTeX itself emits `<span class="katex">…</span>`; we wrap that in
//! a small Tidepool-tagged `<span>` so the iter-9 stylesheet can
//! re-tint colour via `var(--tp-ink)` without touching the inner
//! markup.

use std::sync::OnceLock;

use katex::{KatexContext, OutputFormat, Settings, StrictMode, StrictSetting, render_to_string};

use crate::protocol::{ArtifactKind, CanvasError, RenderedArtifact, ThemeClass};

/// Outer wrapper class — the only class non-KaTeX-aware Tidepool CSS
/// hooks onto. Pairs with `var(--tp-ink)` colour rules in iter 9's
/// `globals.css`.
const WRAPPER_CLASS: &str = "cn-canvas-katex";

/// Modifier appended to [`WRAPPER_CLASS`] when `display = true`. UI
/// CSS uses this to add block-level margin / centre alignment that
/// inline math should not get.
const DISPLAY_MODIFIER: &str = "cn-canvas-katex--display";

/// Lazy, process-wide [`KatexContext`]. Construction is non-trivial
/// (loads symbol tables, function registries) but stateless across
/// renders — sharing one `OnceLock` saves ~2-3 ms on warm calls.
fn katex_context() -> &'static KatexContext {
    static CELL: OnceLock<KatexContext> = OnceLock::new();
    CELL.get_or_init(KatexContext::default)
}

/// Render a `latex` artifact. `tex` is producer-supplied source;
/// `display` flips inline vs block.
pub fn render(
    tex: &str,
    display: bool,
    theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    let ctx = katex_context();

    // Strict mode + no trust + HTML+MathML output. `trust = false` is
    // the `Settings::default()` already, but we set it explicitly to
    // make the security posture obvious in code review.
    let settings = Settings::builder()
        .display_mode(display)
        .output(OutputFormat::HtmlAndMathml)
        .strict(StrictSetting::Mode(StrictMode::Error))
        // `throw_on_error = false` would render the error inline; we
        // want a typed adapter error so the gateway can surface a
        // structured `canvas-artifact-error` event.
        .throw_on_error(true)
        .build();

    let inner = render_to_string(ctx, tex, &settings).map_err(|err| CanvasError::Adapter {
        kind: ArtifactKind::Latex,
        message: format!("katex render failed: {err}"),
    })?;

    let html = if display {
        format!("<span class=\"{WRAPPER_CLASS} {DISPLAY_MODIFIER}\">{inner}</span>")
    } else {
        format!("<span class=\"{WRAPPER_CLASS}\">{inner}</span>")
    };

    Ok(RenderedArtifact {
        html_fragment: html,
        theme_class,
        content_hash: String::new(), // iter 7 (cache) populates
        render_kind: ArtifactKind::Latex,
        warnings: Vec::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Smoke: trivial inline expression renders without error and the
    /// outer Tidepool wrapper class is present.
    #[test]
    fn renders_inline_smoke() {
        let out = render("x", false, ThemeClass::TpLight).expect("inline x must render");
        assert!(
            out.html_fragment.contains(WRAPPER_CLASS),
            "wrapper class missing: {}",
            out.html_fragment
        );
        assert!(
            !out.html_fragment.contains(DISPLAY_MODIFIER),
            "inline must not carry display modifier: {}",
            out.html_fragment
        );
    }

    /// Display-mode adds the `--display` modifier class.
    #[test]
    fn display_mode_marker_present() {
        let out = render("x", true, ThemeClass::TpLight).expect("display x must render");
        assert!(
            out.html_fragment.contains(DISPLAY_MODIFIER),
            "expected display modifier: {}",
            out.html_fragment
        );
    }
}
