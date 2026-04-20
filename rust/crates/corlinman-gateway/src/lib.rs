//! corlinman-gateway — library surface.
//!
//! The binary (`main.rs`) composes these modules into a running server.
//! Keeping the logic in a lib crate lets integration tests drive the gateway
//! without booting the process.

pub mod middleware;
pub mod routes;
pub mod server;
pub mod shutdown;
pub mod state;
pub mod ws;
