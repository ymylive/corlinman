"""Smoke tests — package imports cleanly; no in-band tool-call parser module."""

from __future__ import annotations

from corlinman_agent import (
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
    ToolResult,
)


def test_public_surface_exports_expected_symbols() -> None:
    # Everything the gRPC servicer consumes must be importable from the
    # package root.
    assert ChatStart is not None
    assert DoneEvent is not None
    assert ErrorEvent is not None
    assert ReasoningLoop is not None
    assert TokenEvent is not None
    assert ToolCallEvent is not None
    assert ToolResult is not None


def test_no_legacy_tool_parser_module() -> None:
    # Any ``corlinman_agent.tool_parser`` module is prohibited (plan §14 R5);
    # importing it must fail.
    import importlib

    try:
        importlib.import_module("corlinman_agent.tool_parser")
    except ModuleNotFoundError:
        return
    raise AssertionError("corlinman_agent.tool_parser must not exist anymore")
