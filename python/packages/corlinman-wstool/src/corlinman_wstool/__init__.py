"""``corlinman-wstool`` — distributed tool-execution protocol over WebSocket.

Python port of ``rust/crates/corlinman-wstool``. The Rust crate's local
``jsonrpc_stdio`` plugin runtime spawns a child process and talks
JSON-RPC over stdin/stdout; this crate generalises that idea to a
network socket: a runner (same host or different) dials the gateway,
advertises a set of tools, and serves invocations over a multiplexed
WebSocket connection.

The package is split into two halves that talk the same wire protocol
defined in :mod:`corlinman_wstool.protocol`:

- :mod:`corlinman_wstool.server` — :class:`WsToolServer` hosts the
  ``/wstool/connect`` endpoint inside the gateway.
- :mod:`corlinman_wstool.client` — :class:`WsToolRunner` +
  :class:`ToolHandler` are imported by a runner binary.

The wire protocol is byte-compatible with the Rust crate: a Rust
runner can serve a Python server and vice versa.
"""

from __future__ import annotations

from corlinman_wstool.client import (
    ProgressSink,
    ToolHandler,
    WsToolRunner,
    build_connect_url,
    url_encode,
)
from corlinman_wstool.file_fetcher import (
    DEFAULT_MAX_BYTES,
    FILE_FETCHER_TOOL,
    DiskFileServer,
    FileFetcher,
    FileFetcherError,
    FileReadInfo,
    FileServer,
    FileServerHandler,
    file_server_advert,
    file_server_handler,
)
from corlinman_wstool.protocol import ToolAdvert, WsToolMessage
from corlinman_wstool.registry import ConnHandle, ServerState
from corlinman_wstool.server import WsToolServer, invoke_once
from corlinman_wstool.types import (
    AcceptInfo,
    AuthRejected,
    Disconnected,
    FetchedBlob,
    InternalError,
    InvokeOutcome,
    ProtocolError,
    ToolError,
    ToolFailed,
    Unsupported,
    WsToolConfig,
    WsToolError,
)
from corlinman_wstool.types import TimeoutError_ as Timeout

__all__ = [
    "AcceptInfo",
    "AuthRejected",
    "ConnHandle",
    "DEFAULT_MAX_BYTES",
    "Disconnected",
    "DiskFileServer",
    "FILE_FETCHER_TOOL",
    "FetchedBlob",
    "FileFetcher",
    "FileFetcherError",
    "FileReadInfo",
    "FileServer",
    "FileServerHandler",
    "InternalError",
    "InvokeOutcome",
    "ProgressSink",
    "ProtocolError",
    "ServerState",
    "Timeout",
    "ToolAdvert",
    "ToolError",
    "ToolFailed",
    "ToolHandler",
    "Unsupported",
    "WsToolConfig",
    "WsToolError",
    "WsToolMessage",
    "WsToolRunner",
    "WsToolServer",
    "build_connect_url",
    "file_server_advert",
    "file_server_handler",
    "invoke_once",
    "url_encode",
]
