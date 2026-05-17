"""Crate-level error type and :class:`JsonRpcError` mapping.

:class:`McpError` is the internal exception thrown throughout the
package. It carries enough taxonomy for the dispatcher to map onto
JSON-RPC 2.0 error codes (§5.1) plus the corlinman-extension codes
pinned in :mod:`corlinman_mcp_server.types.error_codes`.

Mapping (mirrors Rust ``McpError``):

================================  =============  ==============================
``McpError`` subclass             JSON-RPC code  Spec / origin
================================  =============  ==============================
``McpTransportError``             -32603         Internal error (transport)
``McpAuthError``                  -32603         Auth lives pre-upgrade
``McpSessionNotInitializedError`` -32002         corlinman extension
``McpToolNotAllowedError``        -32001         corlinman extension
``McpMethodNotFoundError``        -32601         JSON-RPC §5.1
``McpInvalidParamsError``         -32602         JSON-RPC §5.1
``McpInvalidRequestError``        -32600         JSON-RPC §5.1
``McpParseError``                 -32700         JSON-RPC §5.1
``McpInternalError``              -32603         JSON-RPC §5.1
================================  =============  ==============================
"""

from __future__ import annotations

from .types import JsonRpcError, JsonValue, error_codes


class McpError(Exception):
    """Common base class for every error variant raised by adapters,
    dispatcher and transport.

    Catching :class:`McpError` is the Python equivalent of matching on
    the Rust enum.
    """

    _wire_message_prefix: str = ""

    def jsonrpc_code(self) -> int:  # pragma: no cover — overridden
        raise NotImplementedError

    def to_jsonrpc_error(self) -> JsonRpcError:
        """Convert this exception into a wire-shaped :class:`JsonRpcError`."""
        return JsonRpcError.new(self.jsonrpc_code(), self._wire_message())

    def _wire_message(self) -> str:
        # Default mirrors the Rust ``Display`` impl: ``<prefix>: <args>``.
        suffix = super().__str__()
        if self._wire_message_prefix:
            return f"{self._wire_message_prefix}: {suffix}" if suffix else self._wire_message_prefix
        return suffix


class McpTransportError(McpError):
    """Transport / framing failure (oversized frame, websocket close,
    malformed UTF-8). Never raised by an adapter — only the transport
    layer emits this."""

    _wire_message_prefix = "transport"

    def jsonrpc_code(self) -> int:
        return error_codes.INTERNAL_ERROR


class McpAuthError(McpError):
    """In-band auth denial (e.g. resource-scheme mismatch). Pre-upgrade
    auth is HTTP 401, not this variant."""

    _wire_message_prefix = "auth"

    def jsonrpc_code(self) -> int:
        return error_codes.INTERNAL_ERROR


class McpSessionNotInitializedError(McpError):
    """Client sent a non-``initialize`` method while the session is in
    ``Connected`` state. JSON-RPC code -32002 (corlinman extension)."""

    def __init__(self) -> None:
        super().__init__("session not initialized; expected `initialize` first")

    def _wire_message(self) -> str:
        return "session not initialized; expected `initialize` first"

    def jsonrpc_code(self) -> int:
        return error_codes.SESSION_NOT_INITIALIZED


class McpToolNotAllowedError(McpError):
    """Token's ``tools_allowlist`` rejects the requested tool name.
    JSON-RPC code -32001 (corlinman extension)."""

    _wire_message_prefix = "tool not allowed"

    def __init__(self, tool_name: str) -> None:
        super().__init__(tool_name)
        self.tool_name = tool_name

    def jsonrpc_code(self) -> int:
        return error_codes.TOOL_NOT_ALLOWED


class McpMethodNotFoundError(McpError):
    """Method string doesn't match any known capability route.
    JSON-RPC code -32601."""

    _wire_message_prefix = "method not found"

    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method

    def jsonrpc_code(self) -> int:
        return error_codes.METHOD_NOT_FOUND


class McpInvalidParamsError(McpError):
    """``params`` failed to deserialise, or carried a value the adapter
    can't fulfil (unknown resource URI, unknown prompt name, etc.).
    JSON-RPC code -32602. Optional ``data`` payload echoes the offending
    value back to the client."""

    def __init__(self, message: str, data: JsonValue | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def _wire_message(self) -> str:
        return f"invalid params: {self.message}"

    def jsonrpc_code(self) -> int:
        return error_codes.INVALID_PARAMS

    def to_jsonrpc_error(self) -> JsonRpcError:
        # InvalidParams is the one variant that *strips* the prefix and
        # uses the bare ``message`` on the wire (mirrors Rust ``From``
        # impl). The optional ``data`` field carries the offending value.
        err = JsonRpcError.new(self.jsonrpc_code(), self.message)
        if self.data is not None:
            err = err.with_data(self.data)
        return err


class McpInvalidRequestError(McpError):
    """Request envelope malformed (bad ``jsonrpc`` literal, missing
    ``method``, etc.). JSON-RPC code -32600."""

    _wire_message_prefix = "invalid request"

    def jsonrpc_code(self) -> int:
        return error_codes.INVALID_REQUEST


class McpParseError(McpError):
    """Inbound bytes weren't valid JSON. JSON-RPC code -32700."""

    _wire_message_prefix = "parse error"

    def jsonrpc_code(self) -> int:
        return error_codes.PARSE_ERROR


class McpInternalError(McpError):
    """Catch-all for adapter-internal failures (DB, plugin runtime
    panic surfaced as Result, etc.). JSON-RPC code -32603."""

    _wire_message_prefix = "internal"

    def jsonrpc_code(self) -> int:
        return error_codes.INTERNAL_ERROR


__all__ = [
    "McpAuthError",
    "McpError",
    "McpInternalError",
    "McpInvalidParamsError",
    "McpInvalidRequestError",
    "McpMethodNotFoundError",
    "McpParseError",
    "McpSessionNotInitializedError",
    "McpToolNotAllowedError",
    "McpTransportError",
]
