"""Tests for ``corlinman_channels.logstream``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from corlinman_channels.common import ConfigError
from corlinman_channels.logstream import (
    LogFrame,
    LogStreamAdapter,
    LogStreamConfig,
    parse_frame,
)
from websockets.asyncio.server import ServerConnection


class TestParseFrame:
    """Frame parser is forward-compatible — unknown keys land in ``fields``."""

    def test_known_keys_round_trip(self) -> None:
        f = parse_frame({
            "stream": "agent.brain",
            "level": "info",
            "message": "turn started",
            "ts": 1700000000,
        })
        assert f.stream == "agent.brain"
        assert f.level == "info"
        assert f.message == "turn started"
        assert f.ts == 1700000000
        assert f.fields == {}

    def test_unknown_keys_land_in_fields(self) -> None:
        f = parse_frame({
            "stream": "x",
            "user_id": "abc",       # unknown top-level — should land in fields
            "extra": {"k": "v"},
        })
        assert f.fields == {"user_id": "abc", "extra": {"k": "v"}}

    def test_explicit_fields_object_is_merged(self) -> None:
        f = parse_frame({
            "stream": "x",
            "fields": {"foo": 1},
            "bar": 2,
        })
        # `fields` explicit object + top-level extra both land in .fields.
        assert f.fields == {"foo": 1, "bar": 2}

    def test_missing_keys_default(self) -> None:
        f = parse_frame({})
        assert f.stream == ""
        assert f.level == "info"
        assert f.message == ""
        assert f.ts == 0

    def test_string_ts_coerces_to_int(self) -> None:
        # Allows producers to ship "1700000000" without breaking the reader.
        f = parse_frame({"ts": "1700000000"})
        assert f.ts == 1700000000

    def test_invalid_ts_defaults_to_zero(self) -> None:
        f = parse_frame({"ts": "not-a-number"})
        assert f.ts == 0


class TestConfig:
    def test_empty_url_raises(self) -> None:
        with pytest.raises(ConfigError):
            LogStreamAdapter(LogStreamConfig(url=""))


class TestLogStreamIntegration:
    """End-to-end tests against an in-process ``websockets`` server."""

    async def test_yields_normalized_event_per_frame(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({
                "stream": "agent.brain",
                "level": "info",
                "message": "turn started",
                "ts": 1_700_000_000,
            }))
            await ws.send(json.dumps({
                "stream": "agent.brain",
                "level": "warn",
                "message": "rate limited",
                "ts": 1_700_000_001,
                "reason": "qps",
            }))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = LogStreamAdapter(LogStreamConfig(url=url, account="tenant-a"))
            async with adapter:
                collected: list[Any] = []

                async def pull_two() -> None:
                    async for ev in adapter.inbound():
                        collected.append(ev)
                        if len(collected) == 2:
                            return

                await asyncio.wait_for(pull_two(), timeout=5.0)

        assert len(collected) == 2
        first, second = collected
        assert first.channel == "logstream"
        assert first.binding.account == "tenant-a"
        assert first.binding.thread == "agent.brain"
        # session_key() is stable per (channel, account, thread, sender)
        # — both frames share a stream so they share a key.
        assert first.binding.session_key() == second.binding.session_key()
        assert first.text == "turn started"
        assert second.text == "rate limited"
        assert isinstance(second.payload, LogFrame)
        assert second.payload.level == "warn"
        assert second.payload.fields.get("reason") == "qps"

    async def test_malformed_frames_are_skipped(self, ws_server) -> None:
        async def handler(ws: ServerConnection) -> None:
            # Send garbage text + valid frame; only the valid one should
            # surface as a normalized event.
            await ws.send("not json at all")
            await ws.send(json.dumps([1, 2, 3]))  # not an object
            await ws.send(json.dumps({"stream": "x", "message": "ok"}))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            adapter = LogStreamAdapter(LogStreamConfig(url=url))
            async with adapter:
                async def first() -> Any:
                    async for ev in adapter.inbound():
                        return ev
                    return None

                ev = await asyncio.wait_for(first(), timeout=5.0)
                assert ev is not None
                assert ev.text == "ok"
                assert ev.binding.thread == "x"
