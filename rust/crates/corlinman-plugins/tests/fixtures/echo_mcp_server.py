#!/usr/bin/env python3
"""
Minimal MCP server used as the iter-10 E2E fixture.

Implements just enough of the protocol for the corlinman-plugins
McpAdapter round-trip:

  - `initialize`            -> serverInfo + protocolVersion + tools-cap
  - `notifications/initialized` (no response; consumed silently)
  - `tools/list`            -> two tools: echo, read_fixture
  - `tools/call`            -> echo: returns the input args verbatim;
                               read_fixture: opens a path argument and
                               returns its bytes as text content
  - `shutdown` / `exit`     -> stdin close terminates the process

Why Python: the existing `tests/jsonrpc_sync.rs` already requires
Python on PATH (it's the canonical scripting fixture language for
this crate's E2E tests). Re-using it here means iter 10 has zero
additional CI prerequisites. A real MCP server (npx or uvx) is
preferred when available; this fixture is the hermetic fallback
that keeps the round-trip honest in CI without node/npm.

Wire format: line-delimited JSON-RPC 2.0 over stdio. One frame per
line; no Content-Length headers (matches the MCP stdio transport).
"""

import json
import os
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(req_id, result) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def respond_error(req_id, code: int, message: str) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
    )


def handle_initialize(req_id) -> None:
    respond(
        req_id,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "echo-mcp-fixture", "version": "0.1.0"},
        },
    )


def handle_tools_list(req_id) -> None:
    respond(
        req_id,
        {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo the supplied arguments back as text content.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                },
                {
                    "name": "read_fixture",
                    "description": "Read a UTF-8 file from disk and return its contents.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "always_error",
                    "description": "Returns isError=true; used to test the error projection.",
                    "inputSchema": {"type": "object"},
                },
            ]
        },
    )


def handle_tools_call(req_id, params) -> None:
    name = params.get("name")
    args = params.get("arguments", {}) or {}
    if name == "echo":
        text = args.get("text", "")
        respond(
            req_id,
            {
                "content": [
                    {"type": "text", "text": f"echo: {text}"}
                ],
                "isError": False,
            },
        )
        return
    if name == "read_fixture":
        path = args.get("path")
        if not path:
            respond(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": "missing path argument"}
                    ],
                    "isError": True,
                },
            )
            return
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            respond(
                req_id,
                {
                    "content": [{"type": "text", "text": body}],
                    "isError": False,
                },
            )
        except OSError as exc:
            respond(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": f"open failed: {exc}"}
                    ],
                    "isError": True,
                },
            )
        return
    if name == "always_error":
        respond(
            req_id,
            {
                "content": [{"type": "text", "text": "by design"}],
                "isError": True,
            },
        )
        return
    respond_error(req_id, -32601, f"unknown tool: {name!r}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Ignore malformed frames; the corlinman client never
            # sends them — guards against accidental terminal input.
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            handle_initialize(req_id)
        elif method == "notifications/initialized":
            # Notification — no response.
            continue
        elif method == "tools/list":
            handle_tools_list(req_id)
        elif method == "tools/call":
            handle_tools_call(req_id, params)
        elif method == "shutdown":
            respond(req_id, {})
            break
        else:
            if req_id is not None:
                respond_error(req_id, -32601, f"unknown method: {method!r}")


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        os._exit(0)
