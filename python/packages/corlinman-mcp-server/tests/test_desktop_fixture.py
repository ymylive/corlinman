"""Replay the recorded Claude-Desktop session through the live MCP
server and assert every server frame matches the fixture shape (modulo
``id`` + ``serverInfo.version``).

Mirrors ``tests/desktop_fixture.rs`` 1:1. The fixture file lives at
``tests/fixtures/desktop_2024_11_05.json`` — same bytes as the Rust
crate ships.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import websockets

from corlinman_mcp_server import (
    AdapterDispatcher,
    McpServer,
    McpServerConfig,
    PluginOutputSuccess,
    PromptsAdapter,
    ResourcesAdapter,
    ServerInfo,
    TokenAcl,
    ToolsAdapter,
)

from .conftest import (
    StubPluginRegistry,
    StubPluginRuntime,
    StubSkill,
    StubSkillRegistry,
    make_plugin_entry,
)


def _redact_paths(value, paths: list[str]):
    out = copy.deepcopy(value)
    for p in paths:
        parts = p.split(".")
        _delete_path(out, parts)
    return out


def _delete_path(v, parts: list[str]) -> None:
    if not parts:
        return
    if len(parts) == 1:
        if isinstance(v, dict):
            v.pop(parts[0], None)
        return
    if isinstance(v, dict):
        child = v.get(parts[0])
        if child is not None:
            _delete_path(child, parts[1:])


def _strip_id(v):
    if isinstance(v, dict):
        v = {k: val for k, val in v.items() if k != "id"}
    return v


def _frames_match(recorded, live, ignore_paths: list[str]) -> bool:
    rec = _redact_paths(recorded, ignore_paths)
    got = _redact_paths(live, ignore_paths)
    rec = _strip_id(rec)
    got = _strip_id(got)
    return rec == got


@pytest.mark.asyncio
async def test_desktop_fixture_replay_matches_every_server_frame():
    # Build the same fixture state the Rust test wires up.
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "search the kb")]))
    runtime = StubPluginRuntime(
        PluginOutputSuccess(content=b'{"results":[]}', duration_ms=5)
    )

    skills = StubSkillRegistry(
        [
            StubSkill(
                name="summarize",
                description="summarise content",
                body_markdown="## summarize\n\nGiven a chunk of text, produce a tight summary.\n",
            )
        ]
    )

    tools = ToolsAdapter.with_runtime(reg, runtime)
    resources = ResourcesAdapter(memory_hosts={}, skills=skills)
    prompts = PromptsAdapter(skills)
    dispatcher = AdapterDispatcher.from_adapters(
        ServerInfo(name="corlinman", version="*"),
        [tools, resources, prompts],
    )

    cfg = McpServerConfig(
        tokens=[TokenAcl.permissive("desktop-token")],
        max_frame_bytes=1_048_576,
    )
    server = McpServer(cfg, dispatcher)
    s = await server.bind(host="127.0.0.1", port=0)
    try:
        port = list(s.sockets)[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/mcp?token=desktop-token"
        async with websockets.connect(url) as ws:
            fixture_path = Path(__file__).parent / "fixtures" / "desktop_2024_11_05.json"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            for ex in fixture["exchanges"]:
                direction = ex["direction"]
                label = ex["label"]
                if direction == "client_to_server":
                    if ex.get("frame_kind") == "ws_close":
                        await ws.close()
                        continue
                    await ws.send(json.dumps(ex["frame"]))
                elif direction == "server_to_client":
                    reply = await ws.recv()
                    assert isinstance(reply, str), f"unexpected ws frame at step '{label}': {reply!r}"
                    live = json.loads(reply)
                    ignore = ex.get("ignore_paths", [])
                    assert _frames_match(ex["frame"], live, ignore), (
                        f"fixture mismatch at step '{label}'\n"
                        f"recorded: {json.dumps(_strip_id(_redact_paths(ex['frame'], ignore)), indent=2)}\n"
                        f"live:     {json.dumps(_strip_id(_redact_paths(live, ignore)), indent=2)}"
                    )
                else:
                    raise AssertionError(f"unknown direction in fixture: {direction}")
    finally:
        s.close()
        await s.wait_closed()


def test_fixture_loads_and_round_trips_through_json():
    fixture_path = Path(__file__).parent / "fixtures" / "desktop_2024_11_05.json"
    f = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert f["exchanges"], "fixture must carry exchanges"
    labels = [e["label"] for e in f["exchanges"]]
    assert "initialize" in labels
    assert "resources_read_reply" in labels
    assert "close" in labels


def test_redact_paths_drops_nested_keys():
    v = {"result": {"serverInfo": {"name": "corlinman", "version": "0.1.0"}}}
    stripped = _redact_paths(v, ["result.serverInfo.version"])
    assert stripped == {"result": {"serverInfo": {"name": "corlinman"}}}
