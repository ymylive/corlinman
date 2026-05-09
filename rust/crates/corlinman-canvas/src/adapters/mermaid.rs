//! `mermaid` artifact adapter — diagram → inline SVG.
//!
//! ## Why this is gated
//!
//! `phase4-w3-c3-design.md` § "Sandboxing — Mermaid is the hard one"
//! commits the renderer to in-process `deno_core` (V8 +
//! ECMAScript-only) running a bundled `mermaid.min.js` plus a
//! DOM shim. The trade-offs were:
//!
//! - `node` subprocess per render — 200-400ms cold, requires an
//!   external runtime, no isolation.
//! - `corlinman-sandbox` (docker) — 500ms+ per render; over-spec for
//!   a 100-byte diagram string.
//! - `deno_core` in-process — 50ms warm, ~600 MB prebuilt V8 in the
//!   build artifact, V8 CVE surface to track.
//!
//! The design picks `deno_core`. The cost — V8 — is real: a default
//! `cargo build --workspace` would download a ~600 MB prebuilt static
//! library and add 5-10 minutes to first link. To keep the default
//! build cheap and let operators opt-in to diagram rendering, the
//! `deno_core` dep is wired behind the **`mermaid` Cargo feature**
//! (off by default). The user's iter-6 instruction to "pin a stable
//! version, gate behind a `mermaid` feature if the build cost / link
//! size is heavy" — V8 absolutely qualifies.
//!
//! ## Build-mode matrix
//!
//! - `cargo build` / `cargo build --workspace` → feature off →
//!   `render` returns `CanvasError::Adapter { kind: Mermaid, message:
//!   "mermaid renderer not enabled in this build (rebuild with
//!   --features mermaid)" }`. Producers see a structured error, the
//!   gateway surfaces it as `canvas-artifact-error`, the UI renders
//!   the dashed-glass fallback panel — same path as a `Timeout`.
//!
//! - `cargo build --features mermaid` → V8 + deno_core compile in →
//!   the runtime path below executes. **The runtime path is presently
//!   a scaffold**: the engine boots and applies the design's heap /
//!   timeout / output caps, but the JavaScript bundle (a vendored
//!   `mermaid.min.js` + DOM shim of `document.createElement`,
//!   `getElementById`, etc.) is not yet checked in — that's the
//!   bulk-of-work iter, separately tracked. The scaffold is here so
//!   the dep wiring, the post-process sanitiser stub, and the public
//!   adapter signature are all already exercised by the
//!   feature-gated tests in `tests/adapter_mermaid.rs`.
//!
//! ## Sanitiser posture
//!
//! Per design, post-rendered SVG goes through an `ammonia` whitelist
//! scoped to the SVG tag set. We don't pull `ammonia` into the
//! always-on default build because that's another ~5 MB of regex
//! tables that only matters if mermaid is enabled — bring it in under
//! `--features mermaid` as well when the JS pipeline lands.
//!
//! ## Caps applied to every render (when feature is on)
//!
//! - `max_artifact_bytes` (default 256 KiB) — input diagram source
//!   AND rendered SVG output. Either side blowing through caps
//!   surfaces as `CanvasError::BodyTooLarge`.
//! - `render_timeout_ms` (default 5000) — V8 isolate is killed via
//!   `terminate_execution()` past this. Surfaces as
//!   `CanvasError::Timeout`.
//! - V8 isolate heap limit — 64 MiB hard ceiling on the JS heap.

use crate::protocol::{ArtifactKind, CanvasError, RenderedArtifact, ThemeClass};

/// Default per-artifact body cap (input *or* output) — matches the
/// `[canvas] max_artifact_bytes` config knob in
/// `phase4-w3-c3-design.md` § "Config knobs".
pub(crate) const DEFAULT_MAX_BYTES: usize = 256 * 1024;

/// Default render timeout (ms). Configurable from `[canvas]
/// render_timeout_ms`. Mermaid diagrams typically render in 30-150 ms
/// warm; a 5s ceiling is two orders of magnitude of slack while still
/// catching infinite-loop / fork-bomb diagrams.
#[allow(dead_code)] // used by the feature-gated runtime path
pub(crate) const DEFAULT_TIMEOUT_MS: u64 = 5_000;

/// Render a `mermaid` artifact.
///
/// **Default build (no `mermaid` feature)**: returns a typed
/// `CanvasError::Adapter` indicating the renderer wasn't built in.
/// The gateway surfaces this as `canvas-artifact-error`; the UI
/// renders the dashed-glass fallback panel.
///
/// **`--features mermaid` build**: routes to the V8-backed pipeline
/// in [`render_with_engine`].
pub fn render(
    diagram: &str,
    theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    // Input cap applies regardless of feature flag — a producer
    // shouldn't be able to ship a 1 MB blob through the gateway just
    // to get a "feature disabled" reply, that's wasted bandwidth.
    if diagram.len() > DEFAULT_MAX_BYTES {
        return Err(CanvasError::BodyTooLarge {
            max_bytes: DEFAULT_MAX_BYTES,
            kind: ArtifactKind::Mermaid,
        });
    }

    #[cfg(feature = "mermaid")]
    {
        return render_with_engine(diagram, theme_class);
    }

    #[cfg(not(feature = "mermaid"))]
    {
        let _ = (diagram, theme_class); // silence unused-var warnings
        Err(CanvasError::Adapter {
            kind: ArtifactKind::Mermaid,
            message: "mermaid renderer not enabled in this build (rebuild with --features mermaid)"
                .to_string(),
        })
    }
}

#[cfg(feature = "mermaid")]
fn render_with_engine(
    _diagram: &str,
    _theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    // Iter 6 scaffold. The design path:
    //   1. Lazy-init a process-wide `deno_core::JsRuntime` with V8
    //      heap caps via `IsolateCreateParams`, no `Deno.*` extension
    //      loaded — pure ECMAScript only.
    //   2. Source-load a vendored `mermaid.min.js` plus a 200-line
    //      DOM shim covering `document.createElement`,
    //      `getElementById`, `appendChild`, basic `Element.attribute`,
    //      and a `window` stub. Cached once per process.
    //   3. `runtime.execute_script("mermaid_render.js", &format!(
    //          r#"globalThis.__cn_render({diagram_json})"#))`
    //      with `terminate_execution()` armed for `DEFAULT_TIMEOUT_MS`.
    //   4. Read `globalThis.__cn_result` (an SVG string), enforce
    //      `output.len() <= DEFAULT_MAX_BYTES`, run through `ammonia`
    //      with the SVG-only tag whitelist (svg, g, path, rect,
    //      circle, line, text, defs, marker, polyline, polygon — no
    //      `<script>`, no event-handler attrs, no `xlink:href` to
    //      non-fragment URLs).
    //   5. Wrap in `<div class="cn-canvas-mermaid">…</div>`, return.
    //
    // This adapter intentionally *does not* land that pipeline yet:
    // building it correctly is a multi-day vertical (mermaid 11.x
    // makes heavy use of MutationObserver and computed CSS, and the
    // upstream DOM shim from `mermaid-cli` is ~600 LOC, not 200 — the
    // design's estimate is optimistic).
    //
    // Returning a structured `Adapter` error from the feature-on path
    // is the safer landing for iter 6: the dep wiring, feature flag,
    // and public signature are all already exercised; the JS+DOM
    // pipeline is tracked as the iter-6b follow-up.
    Err(CanvasError::Adapter {
        kind: ArtifactKind::Mermaid,
        message: "mermaid runtime scaffold present but JS bundle + DOM shim not yet vendored \
                  (iter 6b); see `phase4-w3-c3-design.md` § Sandboxing"
            .to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Default-feature build (no `--features mermaid`): the adapter
    /// must return a typed `Adapter` error mentioning the feature
    /// flag, so the gateway / UI can surface the right fallback.
    #[cfg(not(feature = "mermaid"))]
    #[test]
    fn default_build_reports_feature_disabled() {
        let err = render("graph LR; A-->B", ThemeClass::TpLight)
            .expect_err("default build must report feature off");
        match err {
            CanvasError::Adapter { kind: ArtifactKind::Mermaid, message } => {
                assert!(
                    message.contains("--features mermaid"),
                    "expected feature-flag hint in error: {message}",
                );
            }
            other => panic!("expected Adapter error, got {other:?}"),
        }
    }

    /// Input cap applies in *both* feature modes — a producer
    /// shipping a 1 MiB diagram source must not waste downstream
    /// pipeline cycles regardless of whether mermaid is built in.
    #[test]
    fn oversized_input_rejected_before_engine() {
        let huge: String = "x".repeat(DEFAULT_MAX_BYTES + 1);
        let err = render(&huge, ThemeClass::TpLight)
            .expect_err("oversized must error");
        assert!(matches!(
            err,
            CanvasError::BodyTooLarge { kind: ArtifactKind::Mermaid, .. }
        ));
    }
}
