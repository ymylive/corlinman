"""Integration tests for the multi-scheme ``FileFetcher``.

Mirrors ``rust/crates/corlinman-wstool/tests/file_fetcher.rs``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path

import httpx
import pytest

from corlinman_wstool import (
    DiskFileServer,
    FileFetcher,
    FileFetcherError,
    WsToolRunner,
    file_server_advert,
    file_server_handler,
)
from corlinman_wstool.file_fetcher import (
    FILE_FETCHER_TOOL,
    InvalidUri,
    LocalRootMissing,
    PathTraversal,
    SizeLimit,
    UnknownRunner,
    UnsupportedScheme,
)

from .conftest import Harness


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.mark.asyncio
async def test_fetch_file_uri_roundtrips_small_file_with_hash_match(
    tmp_path: Path,
) -> None:
    payload = b"golden pillow contents"
    p = tmp_path / "hello.txt"
    p.write_bytes(payload)

    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=tmp_path, http_client=http, max_bytes=4 * 1024)
        uri = f"file://{p}"
        blob = await fetcher.fetch(uri)
    assert blob.data == payload
    assert blob.total_bytes == len(payload)
    assert blob.sha256 == _sha256_hex(payload)


@pytest.mark.asyncio
async def test_fetch_file_uri_rejects_path_traversal(tmp_path: Path) -> None:
    inside = tmp_path / "ok.txt"
    inside.write_bytes(b"ok")
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=tmp_path, http_client=http, max_bytes=4 * 1024)
        traversal_uri = f"file://{tmp_path}/../ok.txt"
        with pytest.raises(PathTraversal):
            await fetcher.fetch(traversal_uri)


@pytest.mark.asyncio
async def test_fetch_size_exceeds_limit_errors(tmp_path: Path) -> None:
    p = tmp_path / "big.bin"
    p.write_bytes(b"\x00" * 2048)
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=tmp_path, http_client=http, max_bytes=1024)
        uri = f"file://{p}"
        with pytest.raises(SizeLimit) as excinfo:
            await fetcher.fetch(uri)
    assert excinfo.value.got == 2048
    assert excinfo.value.limit == 1024


@pytest.mark.asyncio
async def test_fetch_unsupported_scheme_errors() -> None:
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=1024)
        with pytest.raises(UnsupportedScheme) as excinfo:
            await fetcher.fetch("ftp://example.com/x")
    assert excinfo.value.scheme == "ftp"


@pytest.mark.asyncio
async def test_fetch_local_root_missing_errors() -> None:
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=1024)
        with pytest.raises(LocalRootMissing):
            await fetcher.fetch("file:///etc/hostname")


@pytest.mark.asyncio
async def test_fetch_file_invalid_uri_relative(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=tmp_path, http_client=http, max_bytes=1024)
        with pytest.raises(InvalidUri):
            await fetcher.fetch("file://relative/path")


@pytest.mark.asyncio
async def test_fetch_unknown_runner_errors(harness: Harness) -> None:
    async with httpx.AsyncClient() as http:
        fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=1024)
        fetcher.with_ws_server(harness.server.state)
        with pytest.raises(UnknownRunner) as excinfo:
            await fetcher.fetch("ws-tool://nope/some/path")
    assert excinfo.value.runner_id == "nope"


@pytest.mark.asyncio
async def test_fetch_ws_tool_uri_from_runner_roundtrips(
    harness: Harness, tmp_path: Path
) -> None:
    payload = b"blob served over ws-tool"
    (tmp_path / "doc.txt").write_bytes(payload)
    disk = DiskFileServer(tmp_path, max_bytes=16 * 1024)
    handler = file_server_handler(disk)

    runner = await WsToolRunner.connect(
        harness.ws_url,
        harness.token,
        "file-runner",
        [file_server_advert()],
    )
    serve = asyncio.create_task(runner.serve_with(handler))
    try:
        # Wait for advert.
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            if FILE_FETCHER_TOOL in harness.server.advertised_tools():
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("file_fetcher tool never registered")
            await asyncio.sleep(0.01)

        async with httpx.AsyncClient() as http:
            fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=16 * 1024)
            fetcher.with_ws_server(harness.server.state)
            blob = await fetcher.fetch("ws-tool://file-runner/doc.txt")
        assert blob.data == payload
        assert blob.total_bytes == len(payload)
        assert blob.sha256 == _sha256_hex(payload)
    finally:
        await runner.close()
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_fetch_http_uri_roundtrips() -> None:
    """Stand up a tiny local HTTP server to verify the http branch.

    We use ``httpx.MockTransport`` so the test doesn't need real socket
    plumbing for HTTP — the file_fetcher accepts any
    ``httpx.AsyncClient``.
    """
    payload = bytes(range(256)) * 4  # 1 KiB of varied bytes

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"content-type": "application/octet-stream"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=64 * 1024)
        blob = await fetcher.fetch("http://example.com/blob")
    assert blob.data == payload
    assert blob.mime == "application/octet-stream"
    assert blob.sha256 == _sha256_hex(payload)


@pytest.mark.asyncio
async def test_fetch_http_size_limit_errors() -> None:
    payload = b"x" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        fetcher = FileFetcher(local_root=None, http_client=http, max_bytes=512)
        with pytest.raises(FileFetcherError):
            await fetcher.fetch("http://example.com/blob")
