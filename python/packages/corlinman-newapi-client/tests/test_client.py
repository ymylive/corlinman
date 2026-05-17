"""respx-driven tests for the corlinman-newapi-client surface.

Mirrors ``rust/crates/corlinman-newapi-client/tests/client_test.rs``:
covers probe (happy + 401 + non-newapi), get_user_self (admin vs user
token), list_channels (filter + empty), test_round_trip (latency + 4xx).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from corlinman_newapi_client import (
    ChannelType,
    NewapiClient,
    NotNewapiError,
    UpstreamError,
    UrlError,
)

BASE = "http://newapi.test"


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_probe_returns_user_when_200() -> None:
    respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": 1,
                    "username": "root",
                    "display_name": "Root",
                    "role": 100,
                    "status": 1,
                },
            },
        )
    )
    respx.get(f"{BASE}/api/status").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"version": "v0.4.0"}}
        )
    )

    async with NewapiClient(BASE, "user-tok", "admin-tok") as c:
        result = await c.probe()

    assert result.user.username == "root"
    assert result.server_version == "v0.4.0"


@pytest.mark.asyncio
@respx.mock
async def test_probe_uses_admin_token_for_user_self() -> None:
    route = respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 1, "username": "root", "role": 100, "status": 1},
            },
        )
    )
    respx.get(f"{BASE}/api/status").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"version": "v0.4.0"}}
        )
    )

    async with NewapiClient(BASE, "user-tok", "admin-tok") as c:
        await c.probe()

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer admin-tok"


@pytest.mark.asyncio
@respx.mock
async def test_probe_returns_unauthorized_on_401() -> None:
    respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )

    async with NewapiClient(BASE, "bad", None) as c:
        with pytest.raises(UpstreamError) as excinfo:
            await c.probe()

    assert excinfo.value.status == 401
    assert "unauthorized" in excinfo.value.body


@pytest.mark.asyncio
@respx.mock
async def test_probe_returns_notnewapi_when_status_endpoint_missing() -> None:
    respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 1, "username": "x", "role": 1, "status": 1},
            },
        )
    )
    respx.get(f"{BASE}/api/status").mock(return_value=httpx.Response(404))

    async with NewapiClient(BASE, "tok", None) as c:
        with pytest.raises(NotNewapiError):
            await c.probe()


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_channels_returns_filtered_by_type() -> None:
    route = respx.get(f"{BASE}/api/channel/", params={"type": "1"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": 10,
                        "name": "openai-primary",
                        "type": 1,
                        "status": 1,
                        "models": "gpt-4o,gpt-4o-mini",
                        "group": "default",
                    },
                    {
                        "id": 11,
                        "name": "openai-fallback",
                        "type": 1,
                        "status": 2,
                        "models": "gpt-4o",
                        "group": "default",
                    },
                ],
            },
        )
    )

    async with NewapiClient(BASE, "tok", None) as c:
        channels = await c.list_channels(ChannelType.LLM)

    assert route.called
    assert len(channels) == 2
    assert channels[0].name == "openai-primary"
    assert "gpt-4o" in channels[0].models


@pytest.mark.asyncio
@respx.mock
async def test_list_channels_returns_empty_on_empty_data() -> None:
    respx.get(f"{BASE}/api/channel/").mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

    async with NewapiClient(BASE, "tok", None) as c:
        channels = await c.list_channels(ChannelType.EMBEDDING)

    assert channels == []


@pytest.mark.asyncio
@respx.mock
async def test_list_channels_filters_embedding_with_type_2() -> None:
    route = respx.get(f"{BASE}/api/channel/", params={"type": "2"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": 20,
                        "name": "emb-bge",
                        "type": 2,
                        "status": 1,
                        "models": "BAAI/bge-large-zh-v1.5",
                        "group": "default",
                    }
                ],
            },
        )
    )

    async with NewapiClient(BASE, "tok", None) as c:
        channels = await c.list_channels(ChannelType.EMBEDDING)

    assert route.called
    assert len(channels) == 1
    assert channels[0].channel_type == 2


# ---------------------------------------------------------------------------
# test_round_trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_test_round_trip_records_latency() -> None:
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )

    async with NewapiClient(BASE, "user-tok", None) as c:
        res = await c.test_round_trip("gpt-4o-mini")

    assert res.status == 200
    assert res.latency_ms < 5000
    assert res.model == "gpt-4o-mini"
    assert route.calls.last.request.headers["Authorization"] == "Bearer user-tok"


@pytest.mark.asyncio
@respx.mock
async def test_test_round_trip_propagates_4xx() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="rate limited")
    )

    async with NewapiClient(BASE, "t", None) as c:
        with pytest.raises(UpstreamError) as excinfo:
            await c.test_round_trip("x")

    assert excinfo.value.status == 429
    assert "rate limited" in excinfo.value.body


@pytest.mark.asyncio
@respx.mock
async def test_test_round_trip_falls_back_to_supplied_model_on_non_json_body() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="not-json")
    )

    async with NewapiClient(BASE, "t", None) as c:
        res = await c.test_round_trip("fallback-model")

    assert res.status == 200
    assert res.model == "fallback-model"


# ---------------------------------------------------------------------------
# get_user_self
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_user_self_uses_admin_token_when_present() -> None:
    route = respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 7, "username": "ops", "role": 100, "status": 1},
            },
        )
    )

    async with NewapiClient(BASE, "user-x", "admin-special") as c:
        u = await c.get_user_self()

    assert u.username == "ops"
    assert route.calls.last.request.headers["Authorization"] == "Bearer admin-special"


@pytest.mark.asyncio
@respx.mock
async def test_get_user_self_falls_back_to_user_token_when_no_admin() -> None:
    route = respx.get(f"{BASE}/api/user/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 7, "username": "ops", "role": 1, "status": 1},
            },
        )
    )

    async with NewapiClient(BASE, "just-user", None) as c:
        u = await c.get_user_self()

    assert u.username == "ops"
    assert route.calls.last.request.headers["Authorization"] == "Bearer just-user"


# ---------------------------------------------------------------------------
# Constructor / URL validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_url() -> None:
    with pytest.raises(UrlError):
        NewapiClient("not a url", "tok", None)


def test_constructor_rejects_empty_url() -> None:
    with pytest.raises(UrlError):
        NewapiClient("", "tok", None)


def test_channel_type_int_codes() -> None:
    assert ChannelType.LLM.as_int() == 1
    assert ChannelType.EMBEDDING.as_int() == 2
    assert ChannelType.TTS.as_int() == 8
