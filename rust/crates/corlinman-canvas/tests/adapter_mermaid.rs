//! Iter 6 — mermaid adapter tests (feature-gated).
//!
//! The full design test matrix
//! (`mermaid_simple_flowchart_renders`, `mermaid_timeout_terminates_v8`,
//! `mermaid_oversized_output_rejected`, `mermaid_script_tag_stripped`)
//! requires the bundled JS+DOM-shim pipeline that is tracked as iter
//! 6b. This file pins the iter-6 contract that is reachable now:
//!
//! - default build returns a typed `CanvasError::Adapter` so the
//!   gateway / UI surface the structured fallback panel,
//! - oversized input is rejected before any engine work,
//! - dispatch from the public `Renderer::render` reaches the mermaid
//!   adapter (no Unimplemented arm).

use corlinman_canvas::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload, Renderer, ThemeClass,
};

fn render_mermaid(diagram: &str) -> Result<corlinman_canvas::RenderedArtifact, CanvasError> {
    Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Mermaid,
        body: ArtifactBody::Mermaid { diagram: diagram.into() },
        idempotency_key: "art_mermaid_test".into(),
        theme_hint: Some(ThemeClass::TpLight),
    })
}

/// Default-feature build: dispatch reaches the mermaid adapter and
/// produces a typed `Adapter` error mentioning the feature flag.
/// This is the iter-6-shipped invariant; the JS+DOM-shim render path
/// (iter 6b) replaces this with an `Ok(RenderedArtifact)`.
#[cfg(not(feature = "mermaid"))]
#[test]
fn mermaid_default_build_returns_feature_disabled_error() {
    let err = render_mermaid("graph LR; A-->B").expect_err("default build must error");
    match err {
        CanvasError::Adapter { kind: ArtifactKind::Mermaid, message } => {
            assert!(
                message.contains("--features mermaid")
                    || message.to_lowercase().contains("not enabled"),
                "expected feature-flag hint, got {message}",
            );
        }
        other => panic!("expected Adapter error, got {other:?}"),
    }
}

/// Oversized diagram source is rejected before the adapter dispatches
/// anywhere. This applies regardless of feature flag — a hostile or
/// buggy producer should not get to spend gateway pipeline cycles on
/// a 1 MiB blob just to learn the renderer is off.
#[test]
fn mermaid_oversized_input_rejected_before_dispatch() {
    // 256 KiB cap + 1 byte. Cheap to allocate.
    let huge = "x".repeat(256 * 1024 + 1);
    let err = render_mermaid(&huge).expect_err("oversized must error");
    match err {
        CanvasError::BodyTooLarge { kind: ArtifactKind::Mermaid, max_bytes } => {
            assert_eq!(max_bytes, 256 * 1024);
        }
        other => panic!("expected BodyTooLarge, got {other:?}"),
    }
}

/// Renderer dispatch is exhaustive — `Mermaid` is wired and never
/// reaches `CanvasError::Unimplemented`. This is the contract iter 6
/// pins regardless of feature state.
#[test]
fn mermaid_dispatch_never_returns_unimplemented() {
    let result = render_mermaid("graph LR; A-->B");
    if let Err(CanvasError::Unimplemented { kind }) = &result {
        panic!("Unimplemented must not be reachable for kind={kind:?}");
    }
}

/// Theme hint is preserved on whatever the adapter returns (success
/// path or — under the default-feature set — the structured error
/// surface). The error variant doesn't carry `theme_class` directly,
/// but the protocol surface accepting the hint should not drop it
/// for unrelated reasons.
#[test]
fn mermaid_theme_hint_round_trips_via_protocol() {
    let payload = CanvasPresentPayload {
        artifact_kind: ArtifactKind::Mermaid,
        body: ArtifactBody::Mermaid { diagram: "graph LR; A-->B".into() },
        idempotency_key: "art_mermaid_theme".into(),
        theme_hint: Some(ThemeClass::TpDark),
    };
    let json = serde_json::to_string(&payload).expect("serialise");
    let restored: CanvasPresentPayload = serde_json::from_str(&json).expect("deserialise");
    assert_eq!(restored.theme_hint, Some(ThemeClass::TpDark));
}
