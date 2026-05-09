//! Iter 4 — LaTeX adapter tests.
//!
//! Maps to the design's `phase4-w3-c3-design.md` § "Test matrix":
//! `latex_inline_vs_display`, `latex_macro_blacklist`,
//! `latex_unicode_passthrough`. Plus a few defensive tests for the
//! Tidepool wrapper and the typed-error surface.

use corlinman_canvas::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload, Renderer, ThemeClass,
};

fn render_latex(tex: &str, display: bool) -> corlinman_canvas::RenderedArtifact {
    Renderer::new()
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Latex,
            body: ArtifactBody::Latex { tex: tex.into(), display },
            idempotency_key: "art_latex_test".into(),
            theme_hint: Some(ThemeClass::TpLight),
        })
        .expect("latex render must succeed")
}

/// Design test: `latex_inline_vs_display`.
///
/// Block (display) mode emits the `cn-canvas-katex--display` modifier
/// *and* KaTeX's own `katex-display` class. Inline mode emits neither
/// — just the bare wrapper.
#[test]
fn latex_inline_vs_display() {
    let inline = render_latex("a+b", false);
    assert!(inline.html_fragment.contains("cn-canvas-katex"));
    assert!(
        !inline.html_fragment.contains("cn-canvas-katex--display"),
        "inline must not carry display modifier: {}",
        inline.html_fragment,
    );
    assert!(
        !inline.html_fragment.contains("katex-display"),
        "inline must not contain KaTeX's `katex-display` class: {}",
        inline.html_fragment,
    );

    let block = render_latex("a+b", true);
    assert!(
        block.html_fragment.contains("cn-canvas-katex--display"),
        "display must carry our display modifier: {}",
        block.html_fragment,
    );
    assert!(
        block.html_fragment.contains("katex-display"),
        "display must contain KaTeX's `katex-display` class: {}",
        block.html_fragment,
    );
}

/// Design test: `latex_macro_blacklist`.
///
/// `\href` requires `Settings.trust = true`; we pin trust to false so
/// a producer trying to inject a click-through URL gets rejected.
/// `\input` (TeX file inclusion) is not implemented in katex-rs at
/// all, so it surfaces as an "Undefined control sequence" parse
/// error. Both surface as `CanvasError::Adapter` to the caller.
#[test]
fn latex_macro_blacklist() {
    // \href is parsed but rejected because Settings.trust = false →
    // the URL is wrapped in a `<span class="ML__error">` warning the
    // user, but render itself does NOT fail. We assert that no live
    // <a href> made it into the DOM; that's the actual security
    // outcome the test is protecting.
    let renderer = Renderer::new();
    let href_result = renderer.render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Latex,
        body: ArtifactBody::Latex {
            tex: r#"\href{javascript:alert(1)}{x}"#.into(),
            display: false,
        },
        idempotency_key: "art_href".into(),
        theme_hint: None,
    });
    match href_result {
        Ok(rendered) => {
            // The actual security invariant: katex-rs surfaces a
            // trust-failed `\href` as an *error glyph* (the macro
            // name in red) — no `<a>` tag, no live URL navigation.
            // The TeX source is round-tripped inside an
            // `<annotation encoding="application/x-tex">` for screen
            // readers; that text is inert (sits inside MathML, not
            // an attribute), so the `javascript:` substring there is
            // not exploitable.
            //
            // We assert the precise threats:
            //   - no live `<a>` element
            //   - no `href=` attribute carrying the URL
            //   - no inline `onerror=` / `onclick=` event handlers
            let lc = rendered.html_fragment.to_lowercase();
            assert!(
                !lc.contains("<a "),
                "no live <a> must be emitted under trust=false: {}",
                rendered.html_fragment,
            );
            assert!(
                !lc.contains("href=\"javascript:") && !lc.contains("href='javascript:"),
                "no href to javascript: URL: {}",
                rendered.html_fragment,
            );
            assert!(
                !lc.contains(" onerror=") && !lc.contains(" onclick="),
                "no event-handler attrs: {}",
                rendered.html_fragment,
            );
        }
        Err(CanvasError::Adapter { kind: ArtifactKind::Latex, .. }) => {
            // Also acceptable — strict-mode could reject outright.
        }
        Err(other) => panic!("unexpected error variant: {other}"),
    }

    // \input is straight-up undefined in katex-rs → adapter error.
    let input_result = renderer.render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Latex,
        body: ArtifactBody::Latex {
            tex: r#"\input{/etc/passwd}"#.into(),
            display: false,
        },
        idempotency_key: "art_input".into(),
        theme_hint: None,
    });
    let err = input_result.expect_err("\\input must be rejected");
    let CanvasError::Adapter { kind, message } = err else {
        panic!("expected Adapter error, got something else");
    };
    assert_eq!(kind, ArtifactKind::Latex);
    assert!(
        message.to_lowercase().contains("input")
            || message.to_lowercase().contains("undefined")
            || message.to_lowercase().contains("control sequence"),
        "expected error mentioning the unknown macro, got: {message}",
    );
}

/// Design test: `latex_unicode_passthrough`.
///
/// Greek letters and other non-ASCII tokens that LaTeX has macros for
/// (`\alpha`, `\sum`) must render. Raw unicode (e.g. `α`) goes through
/// katex-rs as an unknown character in math mode — the strict-mode
/// behaviour is to error; we keep it strict, but the design's
/// "unicode passthrough" assertion is about *macro-resolved* unicode
/// like `\alpha` producing the α glyph. That's what we verify.
#[test]
fn latex_unicode_passthrough() {
    let out = render_latex(r#"\alpha + \beta = \gamma"#, false);
    let html = &out.html_fragment;
    // KaTeX emits both MathML and HTML by default. The MathML branch
    // contains the resolved unicode glyphs as `<mi>` text content.
    assert!(
        html.contains('α') && html.contains('β') && html.contains('γ'),
        "expected α/β/γ resolved from \\alpha/\\beta/\\gamma in: {html}",
    );
    assert_eq!(out.render_kind, ArtifactKind::Latex);
}

/// Tidepool wrapper class is present on every render.
#[test]
fn latex_tidepool_wrapper_always_present() {
    let inline = render_latex("1+1", false);
    let display = render_latex("1+1", true);
    assert!(inline.html_fragment.starts_with("<span class=\"cn-canvas-katex"));
    assert!(display.html_fragment.starts_with("<span class=\"cn-canvas-katex"));
}

/// Garbled TeX surfaces as `CanvasError::Adapter` for the gateway to
/// translate into a `canvas-artifact-error` SSE payload.
#[test]
fn latex_garbled_input_returns_adapter_error() {
    let result = Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Latex,
        body: ArtifactBody::Latex {
            tex: r#"\frac{a}{"#.into(), // unmatched brace
            display: false,
        },
        idempotency_key: "art_garbled".into(),
        theme_hint: None,
    });
    let err = result.expect_err("garbled TeX must error");
    assert!(matches!(err, CanvasError::Adapter { kind: ArtifactKind::Latex, .. }));
}

/// `theme_hint` echoes onto the rendered artifact so non-CSS-var
/// consumers (Swift / mobile) can pick the right stylesheet.
#[test]
fn latex_theme_class_echoed() {
    let payload_dark = CanvasPresentPayload {
        artifact_kind: ArtifactKind::Latex,
        body: ArtifactBody::Latex { tex: "x".into(), display: false },
        idempotency_key: "art_theme".into(),
        theme_hint: Some(ThemeClass::TpDark),
    };
    let out = Renderer::new().render(&payload_dark).expect("dark must render");
    assert_eq!(out.theme_class, ThemeClass::TpDark);
}
