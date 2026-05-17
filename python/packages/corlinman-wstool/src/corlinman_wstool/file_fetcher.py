"""Multi-scheme blob fetcher shared by gateway-side tooling.

Python port of ``rust/crates/corlinman-wstool/src/file_fetcher.rs``.

# Schemes

- ``file:///<path>`` — read from the local filesystem, rejecting any
  path that escapes the configured ``local_root`` (via ``..``
  components or symlink resolution).
- ``http://...`` / ``https://...`` — fetched with ``httpx`` (async),
  subject to the ``max_bytes`` cap.
- ``ws-tool://<runner_id>/<path>`` — dispatched through an existing
  :class:`~corlinman_wstool.server.WsToolServer` by invoking the
  reserved tool name ``__file_fetcher__/read``. Runners that wish to
  serve this URI family advertise the tool and register a
  :class:`FileServer` handler (typically via :func:`file_server_handler`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from corlinman_wstool.client import ProgressSink, ToolHandler
from corlinman_wstool.protocol import ToolAdvert
from corlinman_wstool.registry import ServerState
from corlinman_wstool.server import invoke_once
from corlinman_wstool.types import FetchedBlob, ToolError, WsToolError

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DiskFileServer",
    "FILE_FETCHER_TOOL",
    "FileFetcher",
    "FileFetcherError",
    "FileReadInfo",
    "FileServer",
    "FileServerHandler",
    "file_server_advert",
    "file_server_handler",
]

#: Reserved tool name dispatched over the WsTool transport to serve
#: ``ws-tool://`` URIs.
FILE_FETCHER_TOOL = "__file_fetcher__/read"

#: Default per-fetch size cap (100 MiB).
DEFAULT_MAX_BYTES: int = 100 * 1024 * 1024

_WS_TOOL_INVOKE_TIMEOUT_MS = 30_000


# ---------------------------------------------------------------------------
# Error taxonomy.
# ---------------------------------------------------------------------------


class FileFetcherError(Exception):
    """Base for all errors produced by :class:`FileFetcher`."""


@dataclass
class UnsupportedScheme(FileFetcherError):
    scheme: str

    def __str__(self) -> str:
        return f"unsupported uri scheme: {self.scheme}"


@dataclass
class InvalidUri(FileFetcherError):
    message: str

    def __str__(self) -> str:
        return f"invalid uri: {self.message}"


class LocalRootMissing(FileFetcherError):
    def __str__(self) -> str:
        return "local_root not configured"


@dataclass
class PathTraversal(FileFetcherError):
    path: str

    def __str__(self) -> str:
        return f"path escapes local_root: {self.path}"


@dataclass
class UnknownRunner(FileFetcherError):
    runner_id: str

    def __str__(self) -> str:
        return f"runner not connected: {self.runner_id}"


@dataclass
class SizeLimit(FileFetcherError):
    got: int
    limit: int

    def __str__(self) -> str:
        return f"size limit exceeded: {self.got} > {self.limit}"


@dataclass
class HashMismatch(FileFetcherError):
    expected: str
    got: str

    def __str__(self) -> str:
        return f"hash mismatch (expected {self.expected}, got {self.got})"


@dataclass
class HttpStatus(FileFetcherError):
    status: int

    def __str__(self) -> str:
        return f"http {self.status}"


@dataclass
class Transport(FileFetcherError):
    message: str

    def __str__(self) -> str:
        return f"transport: {self.message}"


# ---------------------------------------------------------------------------
# Public fetcher.
# ---------------------------------------------------------------------------


class FileFetcher:
    """Gateway-side multi-scheme fetcher.

    Cheap to construct; reuse a single :class:`httpx.AsyncClient` across
    sites for connection pooling.
    """

    def __init__(
        self,
        local_root: Path | None,
        http_client: httpx.AsyncClient,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.local_root = Path(local_root) if local_root is not None else None
        self.http_client = http_client
        self.max_bytes = max_bytes
        self.ws_server: ServerState | None = None

    def with_ws_server(self, state: ServerState) -> FileFetcher:
        """Attach a :class:`~corlinman_wstool.registry.ServerState` handle
        so this fetcher can resolve ``ws-tool://`` URIs.
        """
        self.ws_server = state
        return self

    async def fetch(self, uri: str) -> FetchedBlob:
        """Retrieve a blob by URI. See the module docstring for schemes."""
        if uri.startswith("file://"):
            return await self._fetch_file(uri[len("file://") :])
        if uri.startswith("http://") or uri.startswith("https://"):
            return await self._fetch_http(uri)
        if uri.startswith("ws-tool://"):
            return await self._fetch_ws_tool(uri[len("ws-tool://") :])
        scheme = uri.split(":", 1)[0]
        raise UnsupportedScheme(scheme=scheme)

    async def _fetch_file(self, path_part: str) -> FetchedBlob:
        root = self.local_root
        if root is None:
            raise LocalRootMissing()
        if not path_part.startswith("/"):
            raise InvalidUri(
                message=f"file:// URIs must be absolute, got {path_part}"
            )
        candidate = Path(path_part)
        return await _read_within_root(root, candidate, self.max_bytes)

    async def _fetch_http(self, uri: str) -> FetchedBlob:
        try:
            resp = await self.http_client.get(uri)
        except httpx.HTTPError as err:
            raise Transport(message=str(err)) from err
        if not (200 <= resp.status_code < 300):
            raise HttpStatus(status=resp.status_code)
        mime = resp.headers.get("content-type")
        body = resp.content
        if len(body) > self.max_bytes:
            raise SizeLimit(got=len(body), limit=self.max_bytes)
        sha = hashlib.sha256(body).hexdigest()
        return FetchedBlob(
            data=body, mime=mime, sha256=sha, total_bytes=len(body)
        )

    async def _fetch_ws_tool(self, rest: str) -> FetchedBlob:
        state = self.ws_server
        if state is None:
            raise Transport(message="ws-tool fetcher not attached")
        if "/" not in rest:
            raise InvalidUri(
                message=f"ws-tool URI must be ws-tool://<runner>/<path>, got ws-tool://{rest}"
            )
        runner_id, path = rest.split("/", 1)
        if not runner_id:
            raise InvalidUri(
                message=f"ws-tool URI must be ws-tool://<runner>/<path>, got ws-tool://{rest}"
            )
        if runner_id not in state.runners:
            raise UnknownRunner(runner_id=runner_id)

        args = {
            "path": path,
            "max_bytes": self.max_bytes,
            "runner_id": runner_id,
        }
        try:
            payload = await invoke_once(
                state,
                tool=FILE_FETCHER_TOOL,
                args=args,
                timeout_ms=_WS_TOOL_INVOKE_TIMEOUT_MS,
            )
        except WsToolError as err:
            raise Transport(message=str(err)) from err

        if not isinstance(payload, dict):
            raise Transport(message="runner reply was not a JSON object")
        data_b64 = payload.get("data_b64")
        if not isinstance(data_b64, str):
            raise Transport(message="runner omitted data_b64")
        mime = payload.get("mime")
        if mime is not None and not isinstance(mime, str):
            mime = None
        remote_sha = payload.get("sha256")
        total_bytes = payload.get("total_bytes")
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception as err:  # noqa: BLE001
            raise Transport(message=f"base64: {err}") from err
        got_len = len(raw)
        if got_len > self.max_bytes:
            raise SizeLimit(got=got_len, limit=self.max_bytes)
        got_sha = hashlib.sha256(raw).hexdigest()
        if isinstance(remote_sha, str) and remote_sha != got_sha:
            raise HashMismatch(expected=remote_sha, got=got_sha)
        total = max(int(total_bytes) if isinstance(total_bytes, int) else 0, got_len)
        return FetchedBlob(data=raw, mime=mime, sha256=got_sha, total_bytes=total)


# ---------------------------------------------------------------------------
# Runner-side helpers.
# ---------------------------------------------------------------------------


class FileServer(Protocol):
    """Runner-side abstraction that serves the reserved file-fetcher tool.

    Implementors decide what the ``path`` argument means in their
    virtual layout. :class:`DiskFileServer` is the canonical impl.
    """

    async def open(self, path: str) -> FileReadInfo:  # pragma: no cover — protocol
        ...


@dataclass
class FileReadInfo:
    """Result of a successful :meth:`FileServer.open`."""

    data: bytes
    mime: str | None


class DiskFileServer:
    """Reference :class:`FileServer` that reads from a rooted directory
    with symlink-escape protection.
    """

    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    async def open(self, path: str) -> FileReadInfo:
        cleaned = path.lstrip("/")
        candidate = self.root / cleaned
        blob = await _read_within_root(self.root, candidate, self.max_bytes)
        return FileReadInfo(data=blob.data, mime=None)


def file_server_advert() -> ToolAdvert:
    """Advertisement the runner should include in its ``accept`` so the
    gateway's tool index knows it can serve ``ws-tool://<this-runner>/...``
    URIs.
    """
    return ToolAdvert(
        name=FILE_FETCHER_TOOL,
        description="FileFetcher read endpoint (base64-over-JSON)",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["path"],
        },
    )


class FileServerHandler:
    """Adapter implementing :class:`~corlinman_wstool.client.ToolHandler`
    on top of a :class:`FileServer`.
    """

    def __init__(self, server: FileServer) -> None:
        self.server = server

    async def invoke(
        self,
        tool: str,
        args: object,
        progress: ProgressSink,
        cancel: asyncio.Event,
    ) -> object:
        del progress, cancel  # unused for this transport
        if tool != FILE_FETCHER_TOOL:
            raise ToolError(
                code="unsupported",
                message=f"file_server_handler does not serve {tool}",
            )
        if not isinstance(args, dict):
            raise ToolError(code="invalid_args", message="args must be a JSON object")
        path = args.get("path")
        if not isinstance(path, str):
            raise ToolError(code="invalid_args", message="missing string `path`")
        try:
            info = await self.server.open(path)
        except FileFetcherError as err:
            raise ToolError(code="read_failed", message=str(err)) from err
        except OSError as err:
            raise ToolError(code="read_failed", message=str(err)) from err
        sha = hashlib.sha256(info.data).hexdigest()
        total = len(info.data)
        b64 = base64.b64encode(info.data).decode("ascii")
        return {
            "data_b64": b64,
            "mime": info.mime,
            "sha256": sha,
            "total_bytes": total,
        }


def file_server_handler(server: FileServer) -> FileServerHandler:
    """Wrap a :class:`FileServer` as a :class:`ToolHandler`."""
    return FileServerHandler(server)


# Tell the static checker the handler implements the ToolHandler protocol.
_: ToolHandler = FileServerHandler.__new__(FileServerHandler)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Internals — path safety + sized read.
# ---------------------------------------------------------------------------


async def _read_within_root(
    root: Path, candidate: Path, max_bytes: int
) -> FetchedBlob:
    """Canonicalize both ``root`` and ``candidate``, verify the latter
    lives under the former, enforce ``max_bytes``, and read the file
    into a hashed blob. Raises :class:`PathTraversal` on any escape.
    """

    def _do() -> FetchedBlob:
        try:
            root_canon = root.resolve(strict=True)
        except FileNotFoundError as err:
            raise FileNotFoundError(
                f"canonicalize root {root}: {err}"
            ) from err
        # Reject `..` segments up-front so a missing file doesn't
        # swallow a traversal attempt.
        if any(part == ".." for part in candidate.parts):
            raise PathTraversal(path=str(candidate))
        try:
            target_canon = candidate.resolve(strict=True)
        except FileNotFoundError as err:
            raise FileNotFoundError(f"{candidate}: not found") from err
        try:
            target_canon.relative_to(root_canon)
        except ValueError as err:
            raise PathTraversal(path=str(target_canon)) from err

        st = target_canon.stat()
        if st.st_size > max_bytes:
            raise SizeLimit(got=st.st_size, limit=max_bytes)
        with target_canon.open("rb") as fh:
            data = fh.read()
        if len(data) > max_bytes:
            raise SizeLimit(got=len(data), limit=max_bytes)
        sha = hashlib.sha256(data).hexdigest()
        return FetchedBlob(
            data=data, mime=None, sha256=sha, total_bytes=len(data)
        )

    return await asyncio.to_thread(_do)


# Silence "imported but unused" for os.path on platforms where it's unused.
_ = os
