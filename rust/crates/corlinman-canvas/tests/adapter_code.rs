//! Iter 2 — code adapter tests.
//!
//! Asserts the wire-level outputs the gateway and UI bind to. Goldens
//! deferred to iter 8 (full E2E); these are shape-and-class checks.

use corlinman_canvas::{
    ArtifactBody, ArtifactKind, CanvasPresentPayload, Renderer, ThemeClass,
};

fn render_code(language: &str, source: &str) -> corlinman_canvas::RenderedArtifact {
    Renderer::new()
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Code,
            body: ArtifactBody::Code {
                language: language.to_string(),
                source: source.to_string(),
            },
            idempotency_key: "art_test".into(),
            theme_hint: Some(ThemeClass::TpDark),
        })
        .expect("render must succeed")
}

/// `code_round_trip_rust` — recognised language produces
/// `cn-canvas-code-*` token spans inside a `<pre class="cn-canvas-code">`
/// wrapper.
#[test]
fn code_round_trip_rust() {
    let out = render_code("rust", "fn main() { let x = 42; }");
    assert_eq!(out.render_kind, ArtifactKind::Code);
    assert_eq!(out.theme_class, ThemeClass::TpDark);
    assert!(
        out.html_fragment.starts_with("<pre class=\"cn-canvas-code\">"),
        "expected wrapper, got {}",
        out.html_fragment
    );
    assert!(
        out.html_fragment.ends_with("</code></pre>"),
        "expected </code></pre>, got {}",
        out.html_fragment
    );
    assert!(
        out.html_fragment.contains("cn-canvas-code-"),
        "expected at least one classed token span, got {}",
        out.html_fragment
    );
    // Recognised → no warning footer.
    assert!(out.warnings.is_empty(), "unexpected warnings: {:?}", out.warnings);
}

/// Common producer pattern: `language: "rs"` instead of `"rust"`.
/// Syntect handles this via token / extension lookup; assert it works.
#[test]
fn code_language_extension_alias_works() {
    let out = render_code("rs", "fn main(){}");
    assert!(out.warnings.is_empty(), "expected `rs` to resolve, got warnings: {:?}", out.warnings);
    assert!(out.html_fragment.contains("cn-canvas-code-"));
}

/// `code_unsupported_language_fallback` — bogus language → plain
/// `<pre>`, no token classes, warning emitted, no error.
#[test]
fn code_unsupported_language_fallback() {
    let out = render_code("klingon", "qaplaH'a'");
    assert_eq!(out.render_kind, ArtifactKind::Code);
    // Wrapper still present; modifier class signals plain mode.
    assert!(out.html_fragment.contains("cn-canvas-code--plain"));
    // No syntect token classes.
    assert!(
        !out.html_fragment.contains("cn-canvas-code-keyword"),
        "plain fallback must not emit syntect token classes: {}",
        out.html_fragment
    );
    // Source survives unaltered.
    assert!(out.html_fragment.contains("qaplaH&#39;a&#39;"));
    assert_eq!(out.warnings.len(), 1, "expected one warning, got {:?}", out.warnings);
    assert!(out.warnings[0].contains("klingon"));
}

/// `code_html_escape` — even on the syntect path, raw `<script>` in
/// the source must end up text-escaped, never raw markup.
#[test]
fn code_html_escape_syntect_path() {
    let source = "// <script>alert('xss')</script>";
    let out = render_code("rust", source);
    assert!(
        !out.html_fragment.contains("<script>"),
        "raw <script> reached HTML: {}",
        out.html_fragment
    );
    assert!(
        out.html_fragment.contains("&lt;script&gt;")
            || out.html_fragment.contains("&lt;script"),
        "expected HTML-escaped script tag, got {}",
        out.html_fragment
    );
}

/// Plain-fallback path also escapes — the Klingon snippet doesn't
/// have HTML, so synthesise one explicitly.
#[test]
fn code_html_escape_plain_path() {
    let out = render_code("klingon", "<img src=x onerror=alert(1)>");
    assert!(
        !out.html_fragment.contains("<img"),
        "raw <img> reached HTML: {}",
        out.html_fragment
    );
    assert!(out.html_fragment.contains("&lt;img"));
}

/// Theme passthrough — `theme_hint` reaches `theme_class` on the
/// rendered artifact. Iter 9's UI uses this for non-CSS-var clients.
#[test]
fn code_theme_passthrough() {
    let renderer = Renderer::new();

    let dark = renderer
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Code,
            body: ArtifactBody::Code {
                language: "rust".into(),
                source: "fn x(){}".into(),
            },
            idempotency_key: "k1".into(),
            theme_hint: Some(ThemeClass::TpDark),
        })
        .unwrap();
    assert_eq!(dark.theme_class, ThemeClass::TpDark);

    let light = renderer
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Code,
            body: ArtifactBody::Code {
                language: "rust".into(),
                source: "fn x(){}".into(),
            },
            idempotency_key: "k2".into(),
            theme_hint: None, // default
        })
        .unwrap();
    assert_eq!(light.theme_class, ThemeClass::TpLight);

    // Class-only output: same source, same HTML across themes.
    assert_eq!(dark.html_fragment, light.html_fragment);
}

/// Sanity: empty source still renders without panic and without
/// crashing the syntect path.
#[test]
fn code_empty_source_is_safe() {
    let out = render_code("rust", "");
    assert!(out.html_fragment.contains("<pre class=\"cn-canvas-code\""));
    assert!(out.html_fragment.contains("</code></pre>"));
}
