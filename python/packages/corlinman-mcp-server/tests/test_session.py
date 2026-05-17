"""Per-connection session state machine tests. Mirrors
``src/server/session.rs``'s unit tests + ``tests/session_handshake.rs``.
"""

from __future__ import annotations

import pytest

from corlinman_mcp_server import (
    INITIALIZED_NOTIFICATION,
    ClientCapabilities,
    Implementation,
    InitializeParams,
    McpInvalidRequestError,
    McpSessionNotInitializedError,
    ServerCapabilities,
    SessionPhase,
    SessionState,
    initialize_reply,
)


def sample_initialize_params() -> InitializeParams:
    return InitializeParams(
        protocolVersion="2024-11-05",
        capabilities=ClientCapabilities(),
        clientInfo=Implementation(name="claude-desktop", version="0.7.4"),
    )


def test_fresh_session_starts_in_connected_phase():
    s = SessionState()
    assert s.phase() is SessionPhase.CONNECTED
    assert s.client_protocol_version() is None
    assert s.client_name() is None


def test_happy_path_connected_to_initialized_via_two_steps():
    s = SessionState()
    s.observe_initialize(sample_initialize_params())
    assert s.phase() is SessionPhase.INITIALIZING
    assert s.client_protocol_version() == "2024-11-05"
    assert s.client_name() == "claude-desktop"
    assert s.client_version() == "0.7.4"

    s.observe_initialized_notification()
    assert s.phase() is SessionPhase.INITIALIZED


def test_tools_list_before_initialize_returns_session_not_initialized():
    s = SessionState()
    with pytest.raises(McpSessionNotInitializedError):
        s.check_request_allowed("tools/list")


def test_tools_list_during_initializing_phase_still_rejected():
    s = SessionState()
    s.observe_initialize(sample_initialize_params())
    with pytest.raises(McpSessionNotInitializedError):
        s.check_request_allowed("tools/list")


def test_initialize_request_in_initialized_phase_returns_invalid_request():
    s = SessionState()
    s.observe_initialize(sample_initialize_params())
    s.observe_initialized_notification()
    with pytest.raises(McpInvalidRequestError) as exc:
        s.check_request_allowed("initialize")
    assert "already initialized" in str(exc.value)


def test_duplicate_initialize_during_initializing_returns_invalid_request():
    s = SessionState()
    s.observe_initialize(sample_initialize_params())
    with pytest.raises(McpInvalidRequestError):
        s.observe_initialize(sample_initialize_params())


def test_initialized_notification_before_initialize_rejected():
    s = SessionState()
    with pytest.raises(McpSessionNotInitializedError):
        s.observe_initialized_notification()
    assert s.phase() is SessionPhase.CONNECTED


def test_duplicate_initialized_notification_is_idempotent_no_op():
    s = SessionState()
    s.observe_initialize(sample_initialize_params())
    s.observe_initialized_notification()
    # Spec: duplicates are benign.
    s.observe_initialized_notification()
    assert s.phase() is SessionPhase.INITIALIZED


def test_check_notification_allowed_gates_initialized_to_initializing_only():
    s = SessionState()
    with pytest.raises(McpSessionNotInitializedError):
        s.check_notification_allowed(INITIALIZED_NOTIFICATION)
    s.observe_initialize(sample_initialize_params())
    s.check_notification_allowed(INITIALIZED_NOTIFICATION)
    s.observe_initialized_notification()
    # Post-handshake the gate filters duplicates.
    with pytest.raises(McpSessionNotInitializedError):
        s.check_notification_allowed(INITIALIZED_NOTIFICATION)


def test_cancel_notification_allowed_after_handshake_starts():
    s = SessionState()
    with pytest.raises(McpSessionNotInitializedError):
        s.check_notification_allowed("notifications/cancelled")
    s.observe_initialize(sample_initialize_params())
    s.check_notification_allowed("notifications/cancelled")
    s.observe_initialized_notification()
    s.check_notification_allowed("notifications/cancelled")


def test_initialize_reply_pins_protocol_version_and_server_info():
    result = initialize_reply(ServerCapabilities(), "corlinman", "0.1.0")
    assert result.protocol_version == "2024-11-05"
    assert result.server_info.name == "corlinman"
    assert result.server_info.version == "0.1.0"


# --- replicated bits of tests/session_handshake.rs


def test_full_lifecycle_initialize_then_notification_then_tools_list_allowed():
    s = SessionState()
    assert s.phase() is SessionPhase.CONNECTED
    s.check_request_allowed("initialize")
    s.observe_initialize(sample_initialize_params())
    assert s.phase() is SessionPhase.INITIALIZING
    with pytest.raises(McpSessionNotInitializedError):
        s.check_request_allowed("tools/list")
    s.check_notification_allowed(INITIALIZED_NOTIFICATION)
    s.observe_initialized_notification()
    assert s.phase() is SessionPhase.INITIALIZED
    s.check_request_allowed("tools/list")
    s.check_request_allowed("resources/list")
    s.check_request_allowed("prompts/list")
