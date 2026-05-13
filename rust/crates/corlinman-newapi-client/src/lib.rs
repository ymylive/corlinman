//! HTTP client for QuantumNous/new-api admin API.
//!
//! Read-only operations: channel discovery, user/self introspection,
//! connection probe, 1-token round-trip test. corlinman uses this to
//! power the `/admin/newapi` page and the onboard wizard.

pub mod client;
pub mod types;

pub use client::{NewapiClient, NewapiError};
pub use types::{Channel, ChannelType, ProbeResult, TestResult, User};
