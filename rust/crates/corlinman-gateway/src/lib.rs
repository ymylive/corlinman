//! corlinman-gateway — library surface.
//!
//! The binary (`main.rs`) composes these modules into a running server.
//! Keeping the logic in a lib crate lets integration tests drive the gateway
//! without booting the process.

pub mod config_watcher;
pub mod evolution_applier;
pub mod evolution_observer;
pub mod grpc;
pub mod legacy_migration;
pub mod log_broadcast;
pub mod log_retention;
pub mod mcp;
pub mod metrics;
pub mod middleware;
pub mod placeholder;
pub mod py_config;
pub mod routes;
pub mod server;
pub mod services;
pub mod shutdown;
pub mod state;
pub mod telemetry;
pub mod ws;
