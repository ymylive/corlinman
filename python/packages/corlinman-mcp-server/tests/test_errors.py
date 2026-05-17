"""``McpError`` → ``JsonRpcError`` mapping table. Mirrors
``src/error.rs``\\'s unit tests."""

from __future__ import annotations

import json

from corlinman_mcp_server import (
    JsonRpcError,
    JsonRpcResponse,
    McpAuthError,
    McpInternalError,
    McpInvalidParamsError,
    McpInvalidRequestError,
    McpMethodNotFoundError,
    McpParseError,
    McpSessionNotInitializedError,
    McpToolNotAllowedError,
    McpTransportError,
    error_codes,
)


def test_parse_error_maps_to_negative_32700():
    err = McpParseError("unexpected eof")
    rpc = err.to_jsonrpc_error()
    assert rpc.code == error_codes.PARSE_ERROR == -32700
    assert "parse error" in rpc.message
    assert rpc.data is None


def test_invalid_request_maps_to_negative_32600():
    rpc = McpInvalidRequestError("missing method").to_jsonrpc_error()
    assert rpc.code == error_codes.INVALID_REQUEST == -32600


def test_method_not_found_maps_to_negative_32601_and_carries_method_name():
    rpc = McpMethodNotFoundError("tools/bogus").to_jsonrpc_error()
    assert rpc.code == error_codes.METHOD_NOT_FOUND == -32601
    assert "tools/bogus" in rpc.message


def test_invalid_params_maps_to_negative_32602_without_data():
    rpc = McpInvalidParamsError("unknown resource uri").to_jsonrpc_error()
    assert rpc.code == error_codes.INVALID_PARAMS == -32602
    assert rpc.message == "unknown resource uri"
    assert rpc.data is None


def test_invalid_params_preserves_data_payload():
    rpc = McpInvalidParamsError(
        "unknown prompt name", data={"name": "missing-skill"}
    ).to_jsonrpc_error()
    assert rpc.code == -32602
    assert rpc.data == {"name": "missing-skill"}


def test_session_not_initialized_uses_corlinman_extension_code():
    err = McpSessionNotInitializedError()
    assert err.jsonrpc_code() == error_codes.SESSION_NOT_INITIALIZED == -32002
    rpc = err.to_jsonrpc_error()
    assert rpc.code == -32002
    assert "session not initialized" in rpc.message


def test_tool_not_allowed_uses_corlinman_extension_code():
    rpc = McpToolNotAllowedError("kb:search").to_jsonrpc_error()
    assert rpc.code == error_codes.TOOL_NOT_ALLOWED == -32001
    assert "kb:search" in rpc.message


def test_transport_auth_internal_all_collapse_to_negative_32603():
    for err in [
        McpTransportError("ws closed"),
        McpAuthError("scheme not allowed"),
        McpInternalError("db pool exhausted"),
    ]:
        code = err.jsonrpc_code()
        rpc = err.to_jsonrpc_error()
        assert code == error_codes.INTERNAL_ERROR == -32603
        assert rpc.code == -32603


def test_invalid_params_round_trips_through_jsonrpc_error_to_response_envelope():
    rpc = McpInvalidParamsError("bad arg", data={"field": "limit"}).to_jsonrpc_error()
    resp = JsonRpcResponse.err("req-1", rpc)
    wire = resp.model_dump()
    assert wire["error"]["code"] == -32602
    assert wire["error"]["message"] == "bad arg"
    assert wire["error"]["data"] == {"field": "limit"}


def test_error_payload_is_json_dumpable():
    rpc = JsonRpcError.new(error_codes.INTERNAL_ERROR, "oops")
    json.dumps(rpc.model_dump())
