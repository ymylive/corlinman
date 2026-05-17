"""Tests for ``corlinman_providers.plugins.async_task``.

Ported from the inline tokio tests in
``rust/crates/corlinman-plugins/src/async_task.rs``.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_providers.plugins.async_task import (
    AsyncTaskCompletionError,
    AsyncTaskRegistry,
    CompleteError,
)


@pytest.mark.asyncio
async def test_register_then_complete_delivers_payload() -> None:
    reg = AsyncTaskRegistry()
    fut = reg.register("tsk_1")
    assert reg.is_pending("tsk_1")

    reg.complete("tsk_1", {"result": "hello"})
    value = await fut
    assert value["result"] == "hello"
    assert not reg.is_pending("tsk_1")


@pytest.mark.asyncio
async def test_complete_unknown_task_returns_not_found() -> None:
    reg = AsyncTaskRegistry()
    with pytest.raises(AsyncTaskCompletionError) as exc_info:
        reg.complete("nope", {})
    assert exc_info.value.reason == CompleteError.NOT_FOUND


@pytest.mark.asyncio
async def test_complete_twice_second_call_is_not_found() -> None:
    reg = AsyncTaskRegistry()
    fut = reg.register("tsk_2")
    reg.complete("tsk_2", {"n": 1})
    await fut
    with pytest.raises(AsyncTaskCompletionError) as exc_info:
        reg.complete("tsk_2", {"n": 2})
    assert exc_info.value.reason == CompleteError.NOT_FOUND


@pytest.mark.asyncio
async def test_cancelled_future_surfaces_as_waiter_dropped() -> None:
    reg = AsyncTaskRegistry()
    fut = reg.register("tsk_3")
    fut.cancel()
    # Give the loop a tick to settle the cancellation.
    await asyncio.sleep(0)
    with pytest.raises(AsyncTaskCompletionError) as exc_info:
        reg.complete("tsk_3", {})
    assert exc_info.value.reason == CompleteError.WAITER_DROPPED


@pytest.mark.asyncio
async def test_sweep_expired_removes_old_entries() -> None:
    reg = AsyncTaskRegistry()
    reg.register("old")
    await asyncio.sleep(0.05)
    reg.register("new")
    evicted = reg.sweep_expired(ttl_seconds=0.01)
    assert evicted == 1
    assert not reg.is_pending("old")
    assert reg.is_pending("new")


@pytest.mark.asyncio
async def test_cancel_removes_pending_entry() -> None:
    reg = AsyncTaskRegistry()
    reg.register("tsk_cancel")
    assert reg.cancel("tsk_cancel") is True
    assert not reg.is_pending("tsk_cancel")
    assert reg.cancel("tsk_cancel") is False


@pytest.mark.asyncio
async def test_register_same_id_replaces_prior() -> None:
    reg = AsyncTaskRegistry()
    old_fut = reg.register("tsk_dup")
    new_fut = reg.register("tsk_dup")
    # The old future must be cancelled (defensive replace semantics).
    await asyncio.sleep(0)
    assert old_fut.cancelled()
    assert reg.is_pending("tsk_dup")
    reg.complete("tsk_dup", {"ok": True})
    assert (await new_fut) == {"ok": True}
