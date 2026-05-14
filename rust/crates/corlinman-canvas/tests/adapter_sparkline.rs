//! Iter 5 — sparkline adapter tests.
//!
//! Maps to the design's `phase4-w3-c3-design.md` § "Test matrix":
//! `sparkline_4_points`, `sparkline_constant`, `sparkline_empty_rejected`.
//! Plus defensive coverage for the Tidepool wrapper, NaN / oversized
//! input rejection, and the aria-label.

use corlinman_canvas::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload, Renderer, ThemeClass,
};

fn render_spark(values: Vec<f64>, unit: Option<String>) -> corlinman_canvas::RenderedArtifact {
    Renderer::new()
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Sparkline,
            body: ArtifactBody::Sparkline { values, unit },
            idempotency_key: "art_spark_test".into(),
            theme_hint: Some(ThemeClass::TpLight),
        })
        .expect("sparkline render must succeed")
}

/// Design test: `sparkline_4_points` — `[1, 4, 2, 9]` produces a path
/// with 4 segments (1 `M` + 3 `L`) and `min`/`max` define the y-range.
#[test]
fn sparkline_4_points() {
    let out = render_spark(vec![1.0, 4.0, 2.0, 9.0], Some("ms".into()));
    let html = &out.html_fragment;

    // Wrapper present.
    assert!(html.contains("class=\"cn-canvas-spark\""));
    // Inner path with the line class.
    assert!(html.contains("class=\"cn-canvas-spark-line\""));
    // Path: 1 M (move-to) + 3 L (line-to) for a 4-point series.
    let m_count = html.matches('M').count();
    let l_count = html.matches('L').count();
    assert_eq!(
        m_count, 1,
        "expected exactly 1 M (move-to), got {m_count}: {html}"
    );
    assert_eq!(
        l_count, 3,
        "expected exactly 3 L (line-to), got {l_count}: {html}"
    );

    // Min should be at the bottom (high y), max at the top (low y).
    // We can't reliably parse floats without depending on a quoted
    // pattern; instead assert that 9.0 (the max) appears earlier in
    // the path than the smaller values via baseline padding (Y_PAD =
    // 1.0): the max y-coord should be `1.000`.
    assert!(
        html.contains("1.000"),
        "expected y=1.000 (top, max=9): {html}"
    );

    // viewBox carries the right width: 3 segments * 8 step = 24.
    assert!(html.contains("viewBox=\"0 0 24"));
    // ARIA label includes the unit and the values.
    assert!(html.contains("unit: ms"));
    assert_eq!(out.render_kind, ArtifactKind::Sparkline);
}

/// Design test: `sparkline_constant` — all values equal → flat line
/// at the midline (Y_HEIGHT/2 = 12). No `NaN`, no crash.
#[test]
fn sparkline_constant() {
    let out = render_spark(vec![5.0, 5.0, 5.0, 5.0], None);
    let html = &out.html_fragment;
    // Every point projects to y = 12.000. Look for that exact float
    // formatting at least 4 times (one per point).
    let occurrences = html.matches("12.000").count();
    assert!(
        occurrences >= 4,
        "expected ≥4 occurrences of `12.000` (constant midline), got {occurrences}: {html}",
    );
    // No NaN slipped through.
    assert!(!html.contains("NaN"), "NaN reached HTML: {html}");
}

/// Design test: `sparkline_empty_rejected` — < 2 points must error.
#[test]
fn sparkline_empty_rejected() {
    let renderer = Renderer::new();

    // Zero points.
    let zero = renderer.render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Sparkline,
        body: ArtifactBody::Sparkline {
            values: vec![],
            unit: None,
        },
        idempotency_key: "art_spark_empty".into(),
        theme_hint: None,
    });
    let err = zero.expect_err("empty must error");
    assert!(matches!(
        err,
        CanvasError::Adapter {
            kind: ArtifactKind::Sparkline,
            ..
        }
    ));

    // One point.
    let one = renderer.render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Sparkline,
        body: ArtifactBody::Sparkline {
            values: vec![3.15],
            unit: None,
        },
        idempotency_key: "art_spark_one".into(),
        theme_hint: None,
    });
    let err = one.expect_err("1-point must error");
    let msg = format!("{err}");
    assert!(msg.contains("at least 2"), "unexpected error: {msg}");
}

/// Non-finite values (`NaN`, `±Infinity`) must be rejected.
#[test]
fn sparkline_non_finite_rejected() {
    let renderer = Renderer::new();
    for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let result = renderer.render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Sparkline,
            body: ArtifactBody::Sparkline {
                values: vec![1.0, bad, 2.0],
                unit: None,
            },
            idempotency_key: "art_spark_bad".into(),
            theme_hint: None,
        });
        assert!(result.is_err(), "expected error for non-finite value {bad}");
    }
}

/// Series larger than the 1024-point cap is rejected — producer
/// pre-aggregates instead of us silently downsampling. Mirrors design
/// open question #4.
#[test]
fn sparkline_oversized_rejected() {
    let values: Vec<f64> = (0..2000).map(|i| i as f64).collect();
    let result = Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Sparkline,
        body: ArtifactBody::Sparkline { values, unit: None },
        idempotency_key: "art_spark_big".into(),
        theme_hint: None,
    });
    let err = result.expect_err("oversized must error");
    let msg = format!("{err}");
    assert!(msg.contains("1024"), "unexpected error: {msg}");
}

/// HTML escaping on the unit field — a producer that smuggles
/// `<script>` in the unit must not get it into the title or label.
#[test]
fn sparkline_unit_html_escaped() {
    let out = render_spark(vec![1.0, 2.0], Some("<script>alert(1)</script>".into()));
    let html = &out.html_fragment;
    assert!(
        !html.contains("<script>"),
        "raw <script> reached HTML: {html}"
    );
    assert!(html.contains("&lt;script&gt;"));
}

/// `theme_hint` echoes onto the rendered artifact (same contract as
/// every other adapter).
#[test]
fn sparkline_theme_class_echoed() {
    let out = Renderer::new()
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Sparkline,
            body: ArtifactBody::Sparkline {
                values: vec![1.0, 2.0],
                unit: None,
            },
            idempotency_key: "art_spark_theme".into(),
            theme_hint: Some(ThemeClass::TpDark),
        })
        .expect("dark must render");
    assert_eq!(out.theme_class, ThemeClass::TpDark);
}
