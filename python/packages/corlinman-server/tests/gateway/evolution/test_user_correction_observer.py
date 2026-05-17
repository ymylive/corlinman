"""Tests for the user-correction HookBus listener.

Covers the :func:`register_user_correction_listener` wiring:

* A synthetic :class:`HookEvent.MessageReceived` carrying corrective
  text → a USER_CORRECTION signal is inserted with the expected
  event_kind, target, payload, and session_id.
* A whitespace-only message → no signal inserted.
* A non-correction message → no signal inserted.
* The ``on_signal`` callback is invoked exactly once per matched event.
* Listener never blocks the chat path (insert failure logs + drops).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from corlinman_evolution_store import (
    EVENT_USER_CORRECTION,
    EvolutionSignal,
    EvolutionStore,
    SignalsRepo,
)
from corlinman_hooks import HookBus, HookEvent
from corlinman_server.gateway.evolution.signals.user_correction import (
    register_user_correction_listener,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    s = await EvolutionStore.open(tmp_path / "evolution.sqlite")
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def signals_repo(store: EvolutionStore) -> SignalsRepo:
    return SignalsRepo(store.conn)


@pytest_asyncio.fixture
async def bus() -> AsyncIterator[HookBus]:
    b = HookBus(capacity=32)
    yield b
    b.cancel_token().cancel()


async def _wait_for_signals(
    repo: SignalsRepo,
    *,
    min_count: int,
    timeout: float = 2.0,
) -> list[EvolutionSignal]:
    """Poll ``list_since(0, EVENT_USER_CORRECTION, ...)`` until ``min_count``
    rows land, or the timeout expires. Returns the final read.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    rows: list[EvolutionSignal] = []
    while asyncio.get_running_loop().time() < deadline:
        rows = await repo.list_since(0, EVENT_USER_CORRECTION, 100)
        if len(rows) >= min_count:
            return rows
        await asyncio.sleep(0.02)
    return rows


# ─── Happy path ──────────────────────────────────────────────────────


async def test_corrective_message_emits_signal(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    """A user MessageReceived with a corrective phrase → one signal row."""
    listener = register_user_correction_listener(
        bus,
        signals_repo,
        target_resolver=lambda evt: "alice",
    )
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-abc",
                content="Stop using bullet points please",
                metadata={},
            )
        )

        rows = await _wait_for_signals(signals_repo, min_count=1)
        assert len(rows) == 1
        signal = rows[0]
        assert signal.event_kind == EVENT_USER_CORRECTION
        assert signal.target == "alice"
        assert signal.session_id == "sess-abc"
        assert isinstance(signal.payload_json, dict)
        assert signal.payload_json["kind"] == "imperative"
        assert signal.payload_json["weight"] == pytest.approx(0.85)
        assert signal.payload_json["text"].startswith("Stop using")
        assert "matched_pattern" in signal.payload_json
        assert "snippet" in signal.payload_json
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


async def test_on_signal_callback_invoked(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    """``on_signal`` is awaited once per emitted match."""
    seen: list[EvolutionSignal] = []

    async def _capture(signal: EvolutionSignal) -> None:
        seen.append(signal)

    listener = register_user_correction_listener(
        bus,
        signals_repo,
        on_signal=_capture,
        target_resolver=lambda evt: "alice",
    )
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-1",
                content="No, I said use python",
                metadata={},
            )
        )
        rows = await _wait_for_signals(signals_repo, min_count=1)
        assert len(rows) == 1
        # The on_signal callback is fire-and-forget; give the loop a beat.
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert len(seen) == 1
        assert seen[0].event_kind == EVENT_USER_CORRECTION
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


# ─── Negative path: messages that must NOT emit ──────────────────────


@pytest.mark.parametrize("content", ["", "   ", "\n\t  "])
async def test_whitespace_only_emits_no_signal(
    bus: HookBus,
    signals_repo: SignalsRepo,
    content: str,
) -> None:
    listener = register_user_correction_listener(bus, signals_repo)
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-x",
                content=content,
                metadata={},
            )
        )
        # Give the listener a moment to (not) write.
        await asyncio.sleep(0.05)
        rows = await signals_repo.list_since(0, EVENT_USER_CORRECTION, 100)
        assert rows == []
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


async def test_non_correction_message_emits_no_signal(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    listener = register_user_correction_listener(bus, signals_repo)
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-x",
                content="thanks, that worked perfectly",
                metadata={},
            )
        )
        await asyncio.sleep(0.05)
        rows = await signals_repo.list_since(0, EVENT_USER_CORRECTION, 100)
        assert rows == []
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


async def test_unrelated_event_variant_is_ignored(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    """A ToolCalled event must not be misinterpreted as a user message."""
    listener = register_user_correction_listener(bus, signals_repo)
    try:
        await bus.emit(
            HookEvent.ToolCalled(
                tool="shell",
                runner_id="r1",
                duration_ms=12,
                ok=False,
                error_code="exec_failed",
            )
        )
        await asyncio.sleep(0.05)
        rows = await signals_repo.list_since(0, EVENT_USER_CORRECTION, 100)
        assert rows == []
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


# ─── Target resolver behaviour ───────────────────────────────────────


async def test_target_resolver_failure_leaves_target_none(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    def _bad_resolver(_: Any) -> str:
        raise RuntimeError("resolver boom")

    listener = register_user_correction_listener(
        bus, signals_repo, target_resolver=_bad_resolver
    )
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-1",
                content="You always do that wrong",
                metadata={},
            )
        )
        rows = await _wait_for_signals(signals_repo, min_count=1)
        assert len(rows) == 1
        assert rows[0].target is None
        assert rows[0].event_kind == EVENT_USER_CORRECTION
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass


async def test_no_target_resolver_yields_null_target(
    bus: HookBus,
    signals_repo: SignalsRepo,
) -> None:
    listener = register_user_correction_listener(bus, signals_repo)
    try:
        await bus.emit(
            HookEvent.MessageReceived(
                channel="ws",
                session_key_="sess-2",
                content="I hate when you do that",
                metadata={},
            )
        )
        rows = await _wait_for_signals(signals_repo, min_count=1)
        assert len(rows) == 1
        assert rows[0].target is None
        assert rows[0].session_id == "sess-2"
    finally:
        listener.cancel()
        try:
            await listener
        except (asyncio.CancelledError, BaseException):
            pass
