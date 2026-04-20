//! WebSocket endpoints hosted by the gateway.
//
// TODO: expose a helper `pub fn router() -> Router` wiring axum `Upgrade`
//       handlers for each submodule.

pub mod logstream;
