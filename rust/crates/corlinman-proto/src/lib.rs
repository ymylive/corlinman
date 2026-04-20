//! corlinman-proto — tonic-generated gRPC client + server stubs.
//!
//! All generated types live under the `corlinman::v1` module; downstream
//! crates re-import via `use corlinman_proto::v1::{Message, Role, …}` or
//! `use corlinman_proto::v1::agent::{agent_server::AgentServer, …}`.

// `tonic::include_proto!` emits generated code that is outside our control;
// we silence style lints for it rather than policing tonic's output.
#[allow(
    clippy::large_enum_variant,
    clippy::doc_lazy_continuation,
    clippy::derive_partial_eq_without_eq,
    clippy::all
)]
pub mod v1 {
    //! Contents of `corlinman.v1` package (all 6 proto files compile into here).
    tonic::include_proto!("corlinman.v1");
}
