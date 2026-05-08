//! Per-kind renderer adapters.
//!
//! Each adapter is a pure function `(body, theme_hint) ->
//! Result<RenderedArtifact, CanvasError>`. Adapters share no state;
//! anything expensive (a `SyntaxSet`, a `deno_core::JsRuntime`)
//! lives on the [`crate::Renderer`] handle and is passed in.

pub mod code;
