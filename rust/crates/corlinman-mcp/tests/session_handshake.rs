//! Integration test: full handshake sequence through the public API.
//!
//! Unit tests in `src/server/session.rs` cover the per-transition
//! contract; this file exercises the canonical client→server frame
//! sequence using only the re-exports listed in `lib.rs`, so a
//! regression that breaks the public surface gets caught here.

use corlinman_mcp::schema::{
    ClientCapabilities, Implementation, InitializeParams, ServerCapabilities,
};
use corlinman_mcp::server::{initialize_reply, INITIALIZED_NOTIFICATION};
use corlinman_mcp::{McpError, SessionPhase, SessionState};

fn desktop_initialize() -> InitializeParams {
    InitializeParams {
        protocol_version: "2024-11-05".into(),
        capabilities: ClientCapabilities::default(),
        client_info: Implementation {
            name: "claude-desktop".into(),
            version: "0.7.4".into(),
        },
    }
}

#[test]
fn full_lifecycle_initialize_then_notification_then_tools_list_allowed() {
    let mut session = SessionState::new();
    assert_eq!(session.phase(), SessionPhase::Connected);

    // 1. initialize
    session
        .check_request_allowed("initialize")
        .expect("initialize must be admissible from Connected");
    session.observe_initialize(&desktop_initialize()).unwrap();
    assert_eq!(session.phase(), SessionPhase::Initializing);

    // Server would now reply with InitializeResult. Just confirm the
    // helper produces a well-formed reply.
    let reply = initialize_reply(
        ServerCapabilities::default(),
        "corlinman",
        env!("CARGO_PKG_VERSION"),
    );
    assert_eq!(reply.protocol_version, "2024-11-05");

    // 2. tools/list before notification → still rejected.
    assert!(matches!(
        session.check_request_allowed("tools/list"),
        Err(McpError::SessionNotInitialized)
    ));

    // 3. notifications/initialized
    session
        .check_notification_allowed(INITIALIZED_NOTIFICATION)
        .unwrap();
    session.observe_initialized_notification().unwrap();
    assert_eq!(session.phase(), SessionPhase::Initialized);

    // 4. tools/list now allowed.
    session
        .check_request_allowed("tools/list")
        .expect("tools/list must be admissible after handshake");
    session
        .check_request_allowed("resources/list")
        .expect("resources/list must be admissible after handshake");
    session
        .check_request_allowed("prompts/list")
        .expect("prompts/list must be admissible after handshake");
}

#[test]
fn out_of_order_request_before_initialize_returns_session_not_initialized() {
    let session = SessionState::new();
    let err = session
        .check_request_allowed("tools/list")
        .expect_err("must reject pre-initialize requests");
    assert!(matches!(err, McpError::SessionNotInitialized));
    // And the JSON-RPC code wires through to -32002 (corlinman ext).
    let rpc: corlinman_mcp::JsonRpcError = err.into();
    assert_eq!(rpc.code, -32002);
}

#[test]
fn duplicate_initialize_after_handshake_is_invalid_request_not_session_error() {
    let mut session = SessionState::new();
    session.observe_initialize(&desktop_initialize()).unwrap();
    session.observe_initialized_notification().unwrap();
    let err = session
        .check_request_allowed("initialize")
        .expect_err("re-init must fail post-handshake");
    // -32600 (invalid request), not -32002 (session not initialized).
    let rpc: corlinman_mcp::JsonRpcError = err.into();
    assert_eq!(rpc.code, -32600);
}
