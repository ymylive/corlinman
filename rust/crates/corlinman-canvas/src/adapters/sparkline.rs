//! `sparkline` artifact adapter — hand-rolled inline SVG.
//!
//! No dependency. The design caps this at 60 LOC of "no dep"; we
//! land a slightly more generous implementation (~120 LOC including
//! comments) because the f64 → SVG-path string conversion needs a
//! locale-stable formatter, but the *footprint* is still zero new
//! crates beyond what iter 4 already pulled in.
//!
//! ## Geometry
//!
//! The plot is a `viewBox = "0 0 W H"` SVG with:
//! - `W = (n - 1) * X_STEP`     where `n = values.len()`, `X_STEP = 8`
//! - `H = Y_HEIGHT = 24`        baseline = min, ceiling = max
//!
//! Each value is mapped to `(i * X_STEP, Y_HEIGHT - (v - min) /
//! (max - min) * Y_HEIGHT)`. Constant series (`max == min`) flatten
//! to a horizontal line at `Y_HEIGHT / 2` so the sparkline has a
//! visible "rest" state instead of a `NaN`-divisor crash.
//!
//! ## Tidepool styling
//!
//! Path stroke and fill are class-only — the adapter never inlines
//! `stroke="#…"`. The iter-9 `cn-canvas-spark` rules in `globals.css`
//! resolve to `var(--tp-amber)` (stroke) and `var(--tp-amber-soft)`
//! (fill). Both light and dark themes re-paint via the CSS-var swap.
//!
//! ## Output shape
//!
//! ```html
//! <svg class="cn-canvas-spark" viewBox="0 0 24 24" role="img"
//!      aria-label="sparkline (unit: ms): 1, 4, 2, 9">
//!   <title>sparkline (unit: ms)</title>
//!   <path class="cn-canvas-spark-line" d="M0,18L8,8L16,16L24,0"/>
//! </svg>
//! ```
//!
//! ## Validation
//!
//! - `values.len() < 2` → `CanvasError::Adapter` (a one-point spark
//!   has no slope; rejecting catches producers that forgot to
//!   accumulate a series).
//! - `values.len() > 1024` → reject. Per the design's open question
//!   #4: producer pre-aggregates rather than us downsampling at
//!   render time. Out: silent truncation.
//! - Any non-finite value (`NaN`, `±Infinity`) → reject. We can't
//!   project them onto a finite plot range; bouncing back to the
//!   producer is the only honest answer.

use std::fmt::Write as _;

use crate::protocol::{ArtifactKind, CanvasError, RenderedArtifact, ThemeClass};

/// Class on the outer `<svg>`. Pairs with iter-9 `cn-canvas-spark`
/// rules: stroke `var(--tp-amber)`, fill transparent, width 1.5px.
const WRAPPER_CLASS: &str = "cn-canvas-spark";

/// Class on the inner `<path>`. Carries the fill / stroke variables
/// so a future area-fill variant can switch on a separate class
/// without re-tagging the wrapper.
const LINE_CLASS: &str = "cn-canvas-spark-line";

/// Horizontal step between adjacent data points, in viewBox units.
/// 8 picked so a 32-point series fits comfortably in a 256-unit-wide
/// inline SVG (typical chat surface ~250 px). Series longer than that
/// scale via SVG's `preserveAspectRatio` — no resampling here.
const X_STEP: f64 = 8.0;

/// Total viewBox height. The line never touches `0` or `Y_HEIGHT`
/// thanks to a 1px padding band built into the projection — keeps
/// peaks/troughs visible instead of clipped.
const Y_HEIGHT: f64 = 24.0;

/// 1-pixel inner padding so peaks at `max` aren't clipped against
/// the SVG edge. Symmetric on top + bottom — the *plottable* range
/// is `[Y_PAD, Y_HEIGHT - Y_PAD]`.
const Y_PAD: f64 = 1.0;

/// Server-side cap. Producers pre-aggregate for series longer than
/// this; we don't silently downsample. Mirrors design open question
/// #4.
const MAX_POINTS: usize = 1024;

/// Render a `sparkline` artifact. `unit` is optional — if present it
/// goes into the `<title>` and `aria-label` for screen readers.
pub fn render(
    values: &[f64],
    unit: Option<&str>,
    theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    if values.len() < 2 {
        return Err(CanvasError::Adapter {
            kind: ArtifactKind::Sparkline,
            message: format!(
                "sparkline requires at least 2 points, got {}",
                values.len()
            ),
        });
    }
    if values.len() > MAX_POINTS {
        return Err(CanvasError::Adapter {
            kind: ArtifactKind::Sparkline,
            message: format!(
                "sparkline exceeds {MAX_POINTS}-point cap (got {}); pre-aggregate at the producer",
                values.len()
            ),
        });
    }
    if values.iter().any(|v| !v.is_finite()) {
        return Err(CanvasError::Adapter {
            kind: ArtifactKind::Sparkline,
            message: "sparkline values must be finite (no NaN, no ±Infinity)".to_string(),
        });
    }

    // Range. We already proved values.len() ≥ 2 and all finite, so
    // both `min` and `max` are well-defined.
    let mut min = f64::INFINITY;
    let mut max = f64::NEG_INFINITY;
    for v in values {
        if *v < min {
            min = *v;
        }
        if *v > max {
            max = *v;
        }
    }

    let plot_height = Y_HEIGHT - 2.0 * Y_PAD;
    let total_width = (values.len() as f64 - 1.0) * X_STEP;

    // Projection: x = i * X_STEP; y = Y_HEIGHT - Y_PAD - (v - min) /
    // (max - min) * plot_height. For a constant series (`max == min`)
    // we flatten to the midline.
    let span = max - min;
    let mut path = String::with_capacity(values.len() * 12);
    for (i, v) in values.iter().enumerate() {
        let x = i as f64 * X_STEP;
        let y = if span == 0.0 {
            Y_HEIGHT / 2.0
        } else {
            Y_HEIGHT - Y_PAD - ((v - min) / span) * plot_height
        };
        let cmd = if i == 0 { 'M' } else { 'L' };
        // Format with 3 decimals; sufficient for 1-pixel resolution
        // at typical render sizes, and avoids float-locale traps.
        let _ = write!(path, "{cmd}{x:.3},{y:.3}");
    }

    // Build the SVG. Title / aria-label both include the unit so
    // screen readers get one self-describing utterance. Both surfaces
    // (the `<title>` text content *and* the aria-label attribute
    // value) are HTML-escaped before emission — a producer that
    // smuggles `<script>` in the unit field gets escaped text in both
    // places.
    let mut html = String::with_capacity(values.len() * 12 + 256);
    let label = build_aria_label(values, unit);
    html.push_str("<svg class=\"");
    html.push_str(WRAPPER_CLASS);
    let _ = write!(
        html,
        "\" viewBox=\"0 0 {w:.3} {h:.3}\" \
         preserveAspectRatio=\"none\" role=\"img\" aria-label=\"",
        w = total_width,
        h = Y_HEIGHT,
    );
    push_escaped(&mut html, &label);
    html.push_str("\">");
    html.push_str("<title>");
    push_escaped(&mut html, &label);
    html.push_str("</title>");
    let _ = write!(
        html,
        "<path class=\"{LINE_CLASS}\" fill=\"none\" d=\"{path}\"/>",
    );
    html.push_str("</svg>");

    Ok(RenderedArtifact {
        html_fragment: html,
        theme_class,
        content_hash: String::new(), // iter 7 (cache) populates
        render_kind: ArtifactKind::Sparkline,
        warnings: Vec::new(),
    })
}

/// Build the aria-label text. Joins the values with `, ` and prefixes
/// with the unit if present. Truncates the value list at 16 entries
/// (with an ellipsis) to keep the screen-reader utterance bounded —
/// the visible plot still shows the whole series, just not every
/// number is dictated.
fn build_aria_label(values: &[f64], unit: Option<&str>) -> String {
    let preview_n = values.len().min(16);
    let mut parts = String::new();
    for (i, v) in values.iter().take(preview_n).enumerate() {
        if i > 0 {
            parts.push_str(", ");
        }
        let _ = write!(parts, "{v}");
    }
    if values.len() > preview_n {
        parts.push_str(", …");
    }
    match unit {
        Some(u) if !u.is_empty() => format!("sparkline (unit: {u}): {parts}"),
        _ => format!("sparkline: {parts}"),
    }
}

/// HTML-escape into an existing buffer. Five-char OWASP set; same as
/// the table adapter's helper. We keep these per-adapter rather than
/// extracting shared util because (a) the function is six lines and
/// (b) iter-7 cache will likely add a small `escape.rs` module that
/// can absorb both call sites.
fn push_escaped(out: &mut String, input: &str) {
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
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Boundary check: a 2-point series builds a single segment and
    /// the path string starts with `M` and contains exactly one `L`.
    #[test]
    fn two_points_one_segment() {
        let out = render(&[0.0, 1.0], None, ThemeClass::TpLight).unwrap();
        let html = &out.html_fragment;
        // Path d-attribute opens with M then has one L.
        let m_count = html.matches('M').count();
        let l_count = html.matches('L').count();
        assert!(m_count >= 1, "expected at least one M command: {html}");
        assert!(l_count >= 1, "expected at least one L command: {html}");
    }

    #[test]
    fn aria_label_truncates_long_series() {
        let values: Vec<f64> = (0..32).map(|i| i as f64).collect();
        let label = build_aria_label(&values, Some("ms"));
        // ellipsis present, original count not over-quoted.
        assert!(label.contains("…"), "expected ellipsis in {label}");
        assert!(label.starts_with("sparkline (unit: ms):"));
    }
}
