"""Stdio JSON-RPC peer used by C2's ``kind = "mcp"`` plugin adapter.

Mirrors the Rust ``client::stdio`` module: each frame is a single line
of UTF-8 JSON terminated by ``\\n`` (newline-delimited JSON, the
convention every MCP-stdio reference server uses).

Two background tasks run per :class:`McpClient`:

1. **reader**: pulls lines off the child's stdout, parses the JSON-RPC
   response, and either resolves a parked :class:`asyncio.Future`
   (response by id) or logs and drops the frame.
2. **writer**: drains an :class:`asyncio.Queue` to the child's stdin.
   Single-writer keeps frames newline-aligned and avoids interleaving
   between concurrent ``call``\\s.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import structlog

from .errors import McpError
from .types import (
    JSONRPC_VERSION,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonValue,
    error_codes,
)

log = structlog.get_logger(__name__)

_DISCONNECTED_MARKER: str = "__mcp_disconnected__:"
"""Marker the reader task uses to encode "child stdout closed" through
the response queue; :meth:`McpClient.call` strips this and lifts to
:class:`McpClientError` ``Disconnected``."""


class McpClientError(McpError):
    """Common base class for stdio-client errors. Distinct from the
    server-side :class:`McpError` subclasses because the C2 plugin
    adapter lifts these into its own envelope.
    """

    def jsonrpc_code(self) -> int:
        return error_codes.INTERNAL_ERROR


class McpClientSpawnError(McpClientError):
    """The child process failed to spawn."""


class McpClientMissingStdioError(McpClientError):
    """``stdin`` / ``stdout`` were not piped — the caller passed a
    misconfigured spawn invocation."""

    def __init__(self, *, stdin: bool, stdout: bool) -> None:
        super().__init__(
            f"child process is missing piped stdio (stdin={stdin}, stdout={stdout})"
        )
        self.stdin = stdin
        self.stdout = stdout


class McpClientWriteError(McpClientError):
    """Failed to write the request frame to the child's stdin."""


class McpClientServerError(McpClientError):
    """Server returned a JSON-RPC error response. Carries the wire
    payload so callers can branch on ``code``."""

    def __init__(self, code: int, message: str, data: JsonValue | None = None) -> None:
        super().__init__(f"server error: {message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class McpClientDisconnected(McpClientError):
    """The reader task observed EOF or a fatal parse error before the
    expected response arrived."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"connection closed before reply: {reason}")
        self.reason = reason


def _id_key(id_value: JsonValue) -> str:
    """Canonicalise a JSON-RPC id into a string for dict keying.
    Matches both string and numeric ids."""
    if isinstance(id_value, str):
        return id_value
    if isinstance(id_value, bool):
        return "true" if id_value else "false"
    if isinstance(id_value, (int, float)):
        return str(id_value)
    if id_value is None:
        return "null"
    return json.dumps(id_value, sort_keys=True)


class McpClient:
    """Outbound MCP client over stdio newline-delimited JSON-RPC.

    Construct via :meth:`connect_stdio` (most callers) or
    :meth:`connect_with_process` (advanced, when you've already built
    an :class:`asyncio.subprocess.Process`).

    Use :meth:`close` to terminate the child cleanly. The client can
    also be used as an async context manager.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is None or process.stdout is None:
            stdin_ok = process.stdin is not None
            stdout_ok = process.stdout is not None
            raise McpClientMissingStdioError(stdin=stdin_ok, stdout=stdout_ok)
        self._process: asyncio.subprocess.Process = process
        self._stdin: asyncio.StreamWriter = process.stdin
        self._stdout: asyncio.StreamReader = process.stdout
        self._tx_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=64)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._next_id: int = 0
        self._closed: bool = False
        # Spawn the two worker tasks.
        self._writer_task: asyncio.Task = asyncio.create_task(self._writer_loop())
        self._reader_task: asyncio.Task = asyncio.create_task(self._reader_loop())

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def connect_stdio(cls, cmd: str, args: Iterable[str] = ()) -> McpClient:
        """Spawn a child process and connect.

        ``cmd`` is the program (e.g. ``"python"``); ``args`` are the
        program arguments. The child is given piped stdin/stdout;
        stderr is also piped but currently drained-and-dropped.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                cmd,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as e:
            raise McpClientSpawnError(f"failed to spawn child: {e}") from e
        return cls(process)

    @classmethod
    async def connect_with_process(
        cls,
        process: asyncio.subprocess.Process,
    ) -> McpClient:
        """Advanced entry: caller supplies a fully-built
        :class:`asyncio.subprocess.Process`. stdin + stdout must be
        piped."""
        return cls(process)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _generate_id(self) -> JsonValue:
        n = self._next_id
        self._next_id += 1
        return f"req-{n}"

    async def call(
        self,
        method: str,
        params: JsonValue = None,
    ) -> JsonValue:
        """Send a request and await the matching response.

        Generates a fresh id; the response's id MUST match. The reply's
        ``result`` is returned on success; a JSON-RPC error frame raises
        :class:`McpClientServerError`.
        """
        return await self.call_with_id(self._generate_id(), method, params)

    async def call_with_id(
        self,
        id_value: JsonValue,
        method: str,
        params: JsonValue = None,
    ) -> JsonValue:
        """Like :meth:`call` but uses a caller-supplied id."""
        key = _id_key(id_value)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[key] = fut

        req = JsonRpcRequest(
            jsonrpc=JSONRPC_VERSION,
            id=id_value,
            method=method,
            params=params,
        )
        try:
            frame = json.dumps(req.model_dump(), ensure_ascii=False)
        except (TypeError, ValueError) as e:
            self._pending.pop(key, None)
            raise McpClientWriteError(f"serialize request: {e}") from e

        try:
            await self._tx_queue.put(frame)
        except Exception as e:
            self._pending.pop(key, None)
            raise McpClientDisconnected("writer task closed") from e

        try:
            resp = await fut
        except asyncio.CancelledError:
            raise McpClientDisconnected("future cancelled") from None

        if "result" in resp:
            return resp["result"]
        err = resp.get("error", {})
        message = err.get("message", "")
        if isinstance(message, str) and message.startswith(_DISCONNECTED_MARKER):
            raise McpClientDisconnected(message[len(_DISCONNECTED_MARKER):])
        raise McpClientServerError(
            code=int(err.get("code", error_codes.INTERNAL_ERROR)),
            message=str(message),
            data=err.get("data"),
        )

    async def notify(self, method: str, params: JsonValue = None) -> None:
        """Send a notification (no id, no response expected)."""
        req = JsonRpcRequest(
            jsonrpc=JSONRPC_VERSION,
            id=None,
            method=method,
            params=params,
        )
        try:
            frame = json.dumps(req.model_dump(), ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise McpClientWriteError(f"serialize notification: {e}") from e
        try:
            await self._tx_queue.put(frame)
        except Exception as e:
            raise McpClientDisconnected("writer task closed") from e

    async def close(self) -> None:
        """Terminate the child process and stop the worker tasks."""
        if self._closed:
            return
        self._closed = True
        # Signal writer to exit.
        try:
            await self._tx_queue.put(None)
        except Exception:
            pass
        # Kill the child if still alive.
        try:
            if self._process.returncode is None:
                self._process.kill()
        except ProcessLookupError:
            pass
        # Wait for tasks to finish.
        for task in (self._writer_task, self._reader_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Fail every still-parked waiter.
        for key, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(
                    {
                        "jsonrpc": JSONRPC_VERSION,
                        "id": None,
                        "error": {
                            "code": error_codes.INTERNAL_ERROR,
                            "message": f"{_DISCONNECTED_MARKER}client closed",
                        },
                    }
                )
            self._pending.pop(key, None)
        try:
            await self._process.wait()
        except Exception:
            pass

    async def __aenter__(self) -> McpClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Worker loops
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        try:
            while True:
                frame = await self._tx_queue.get()
                if frame is None:
                    break
                try:
                    self._stdin.write(frame.encode("utf-8"))
                    self._stdin.write(b"\n")
                    await self._stdin.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as err:
                    log.warning("mcp client: stdin write failed", err=str(err))
                    break
        except asyncio.CancelledError:
            return
        finally:
            try:
                self._stdin.close()
            except Exception:
                pass
            log.debug("mcp client: writer task exiting")

    async def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._stdout.readline()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    log.warning("mcp client: stdout read error", err=str(err))
                    break
                if not line:
                    log.debug("mcp client: stdout EOF")
                    break
                try:
                    decoded = line.decode("utf-8").strip()
                except UnicodeDecodeError as err:
                    log.warning("mcp client: stdout decode error", err=str(err))
                    continue
                if not decoded:
                    continue
                try:
                    parsed = json.loads(decoded)
                except json.JSONDecodeError as err:
                    log.warning(
                        "mcp client: parse failed",
                        err=str(err),
                        line=decoded,
                    )
                    continue
                # Need an id to demux.
                if not isinstance(parsed, dict):
                    continue
                rid = parsed.get("id")
                key = _id_key(rid)
                fut = self._pending.pop(key, None)
                if fut is not None and not fut.done():
                    fut.set_result(parsed)
                else:
                    log.debug(
                        "mcp client: dropped unmatched response",
                        id=key,
                    )
        except asyncio.CancelledError:
            return
        finally:
            # Fail every parked waiter on EOF / error.
            for key, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_result(
                        {
                            "jsonrpc": JSONRPC_VERSION,
                            "id": None,
                            "error": {
                                "code": error_codes.INTERNAL_ERROR,
                                "message": f"{_DISCONNECTED_MARKER}stdout closed",
                            },
                        }
                    )
                self._pending.pop(key, None)


__all__ = [
    "McpClient",
    "McpClientDisconnected",
    "McpClientError",
    "McpClientMissingStdioError",
    "McpClientServerError",
    "McpClientSpawnError",
    "McpClientWriteError",
]
