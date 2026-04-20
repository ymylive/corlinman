//! Tower middleware layers: auth, tracing, approval.
//
// TODO: each submodule exposes a layer fn `pub fn layer(...) -> impl Layer<_, _>` so
//       `server::build_router` can `.layer(trace::layer()).layer(auth::layer(cfg))`.
// TODO: order matters — trace first (outer), then auth, then approval (innermost).

pub mod approval;
pub mod auth;
pub mod trace;
