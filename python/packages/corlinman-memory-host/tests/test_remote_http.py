"""Port of the ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-memory-host/src/remote_http.rs``.

Uses :mod:`respx` (the Python equivalent of ``wiremock``) to mock the
remote endpoints — matches the convention used by
``corlinman-newapi-client``."""

from __future__ import annotations

import httpx
import pytest
import respx
from corlinman_memory_host import (
    HealthStatus,
    MemoryDoc,
    MemoryHostError,
    MemoryQuery,
    RemoteHttpHost,
)

BASE = "http://memory.test"


@pytest.mark.asyncio
@respx.mock
async def test_query_sends_expected_body_and_parses_response() -> None:
    route = respx.post(f"{BASE}/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "id": "r1",
                        "content": "alpha",
                        "score": 0.9,
                        "metadata": {"k": 1},
                    },
                    {
                        "id": "r2",
                        "content": "beta",
                        "score": 0.4,
                        "metadata": {},
                    },
                ]
            },
        )
    )

    async with RemoteHttpHost("remote", BASE, "secret-token") as host:
        hits = await host.query(MemoryQuery(text="alpha", top_k=5))

    assert len(hits) == 2
    assert hits[0].id == "r1"
    assert hits[0].source == "remote"
    assert hits[0].score == pytest.approx(0.9)
    assert hits[0].metadata == {"k": 1}
    # Bearer-token + body assertions.
    last = route.calls.last
    assert last.request.headers["Authorization"] == "Bearer secret-token"
    payload = last.request.read()
    assert b'"text"' in payload
    assert b'"alpha"' in payload
    assert b'"top_k"' in payload


@pytest.mark.asyncio
@respx.mock
async def test_upsert_returns_host_assigned_id() -> None:
    respx.post(f"{BASE}/upsert").mock(
        return_value=httpx.Response(200, json={"id": "remote-42"})
    )

    async with RemoteHttpHost("remote", BASE, None) as host:
        new_id = await host.upsert(MemoryDoc(content="c", metadata={}))
    assert new_id == "remote-42"


@pytest.mark.asyncio
@respx.mock
async def test_query_http_error_propagates() -> None:
    respx.post(f"{BASE}/query").mock(
        return_value=httpx.Response(500, text="kaboom")
    )

    async with RemoteHttpHost("remote", BASE, None) as host:
        with pytest.raises(MemoryHostError) as excinfo:
            await host.query(MemoryQuery(text="x", top_k=3))

    assert "HTTP 500" in str(excinfo.value)
    assert "kaboom" in str(excinfo.value)


@pytest.mark.asyncio
@respx.mock
async def test_health_maps_status_codes_ok() -> None:
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200))
    async with RemoteHttpHost("remote", BASE, None) as host:
        status = await host.health()
    assert status == HealthStatus.ok()


@pytest.mark.asyncio
@respx.mock
async def test_health_maps_status_codes_degraded() -> None:
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(503))
    async with RemoteHttpHost("remote", BASE, None) as host:
        status = await host.health()
    assert status.is_degraded()
    assert "503" in status.detail


@pytest.mark.asyncio
@respx.mock
async def test_delete_uses_path_with_id_and_sends_bearer() -> None:
    route = respx.delete(f"{BASE}/docs/abc-123").mock(
        return_value=httpx.Response(204)
    )

    async with RemoteHttpHost("remote", BASE, "tok") as host:
        await host.delete("abc-123")

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
@respx.mock
async def test_base_url_trailing_slash_is_stripped() -> None:
    # Test the ``rstrip('/')`` behaviour: the wire URL is the same
    # whether or not the caller passes a trailing slash.
    respx.post(f"{BASE}/upsert").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    async with RemoteHttpHost("remote", BASE + "/", None) as host:
        new_id = await host.upsert(MemoryDoc(content="c"))
    assert new_id == "x"
