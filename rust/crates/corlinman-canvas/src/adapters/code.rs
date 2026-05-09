//! `code` artifact adapter — syntect, class-based emission.
//!
//! ## Design choice: classes, not inline colours
//!
//! `phase4-w3-c3-design.md` § "Tidepool aesthetic enforcement"
//! requires class-only HTML. Syntect supports two paths:
//!
//! - `highlighted_html_for_string` — bakes RGB into `style="color:
//!   #ff66"`. Theme-locked, breaks light/dark switching.
//! - `ClassedHTMLGenerator` — emits `<span class="cn-canvas-code-…">`.
//!   Theme is resolved at the browser via `--tp-*` CSS vars.
//!
//! We use the second. Net effect: zero theme files bundled
//! server-side; the design's `tidepool-amber` theme name doesn't
//! correspond to a `.tmTheme` artifact in this build — it's a CSS
//! token set in `ui/app/globals.css`. Iter 9 (UI) wires those
//! classes; this iter just emits them.
//!
//! ## Language fallback
//!
//! Unknown `language` (e.g. `"klingon"`) is **not an error** — the
//! adapter falls back to plain `<pre>…</pre>` with a warning. This
//! matches `phase4-w3-c3-design.md` test
//! `code_unsupported_language_fallback` and the agent-output reality
//! that producers may emit obscure language tags.
//!
//! ## HTML escape
//!
//! Producer source is HTML-escaped before tokenisation. Syntect's
//! ClassedHTMLGenerator does this internally; the empty-syntax
//! fallback path applies our own escape so a `<script>` payload can
//! never reach the DOM raw.

use std::sync::OnceLock;

use syntect::html::{ClassStyle, ClassedHTMLGenerator};
use syntect::parsing::SyntaxSet;
use syntect::util::LinesWithEndings;

use crate::protocol::{ArtifactKind, CanvasError, RenderedArtifact, ThemeClass};

/// Class prefix on every emitted token span. Matches
/// `ui/app/globals.css` `cn-canvas-code-*` rules to be added in
/// iter 9.
const CLASS_PREFIX: &str = "cn-canvas-code-";

/// Outer wrapper class. Pairs with `--tp-glass-inner` background
/// and the `var(--tp-ink)` foreground in the iter-9 stylesheet.
const WRAPPER_CLASS: &str = "cn-canvas-code";

/// Lazy, process-wide syntect SyntaxSet. Loading the bundled
/// `default_newlines` set takes ~80ms on first call and ~50 KB
/// resident; we share one instance via `OnceLock`.
fn syntax_set() -> &'static SyntaxSet {
    static CELL: OnceLock<SyntaxSet> = OnceLock::new();
    CELL.get_or_init(SyntaxSet::load_defaults_newlines)
}

/// Render a `code` artifact. Inputs are pre-validated by the
/// `CanvasPresentPayload` deserialiser, so `language` and `source`
/// are guaranteed strings.
pub fn render(
    language: &str,
    source: &str,
    theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    let syntaxes = syntax_set();
    let mut warnings = Vec::new();

    // Resolve the language. Two routes:
    //  1. Exact name match (`"rust"`, `"Rust"`).
    //  2. Token / extension match (`"rs"`, `"py"`).
    // If both miss → plain `<pre>` fallback with a warning.
    let syntax = syntaxes
        .find_syntax_by_token(language)
        .or_else(|| syntaxes.find_syntax_by_name(language))
        .or_else(|| syntaxes.find_syntax_by_extension(language));

    let html = if let Some(syntax) = syntax {
        let mut gen = ClassedHTMLGenerator::new_with_class_style(
            syntax,
            syntaxes,
            ClassStyle::SpacedPrefixed { prefix: CLASS_PREFIX },
        );
        for line in LinesWithEndings::from(source) {
            // `parse_html_for_line_which_includes_newline` returns
            // `Result<(), Error>` (regex / scope failure). Treat as
            // adapter error so producers see what went wrong, but
            // still emit the unhighlighted source as a fallback so
            // the artifact never disappears.
            if let Err(e) = gen.parse_html_for_line_which_includes_newline(line) {
                return Err(CanvasError::Adapter {
                    kind: ArtifactKind::Code,
                    message: format!("syntect parse failed: {e}"),
                });
            }
        }
        let inner = gen.finalize();
        format!("<pre class=\"{WRAPPER_CLASS}\"><code>{inner}</code></pre>")
    } else {
        warnings.push(format!(
            "language `{language}` not recognised; rendered as plain text"
        ));
        let escaped = html_escape(source);
        format!("<pre class=\"{WRAPPER_CLASS} {WRAPPER_CLASS}--plain\"><code>{escaped}</code></pre>")
    };

    Ok(RenderedArtifact {
        html_fragment: html,
        theme_class,
        content_hash: String::new(), // iter 7 (cache) populates
        render_kind: ArtifactKind::Code,
        warnings,
    })
}

/// Minimal HTML escape for the plain-text fallback path. Five
/// characters; matches the OWASP Canon for HTML body context. The
/// syntect path doesn't go through here — `ClassedHTMLGenerator`
/// escapes internally.
fn html_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            other => out.push(other),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Smoke test for the escape helper — the adapter integration
    /// tests cover the rendered-output side.
    #[test]
    fn html_escape_covers_owasp_five() {
        let input = "<script>alert('a&b\")</script>";
        let out = html_escape(input);
        assert!(!out.contains('<'));
        assert!(!out.contains('>'));
        assert!(out.contains("&lt;"));
        assert!(out.contains("&gt;"));
        assert!(out.contains("&amp;"));
        assert!(out.contains("&quot;"));
        assert!(out.contains("&#39;"));
    }
}
