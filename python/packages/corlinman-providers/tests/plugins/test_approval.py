"""Tests for ``corlinman_providers.plugins.approval``.

These cover the Python implementation only (the Rust source is a TODO stub).
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_providers.plugins.approval import (
    ApprovalDecision,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStore,
)


@pytest.mark.asyncio
async def test_store_insert_and_pending() -> None:
    store = ApprovalStore()
    req = ApprovalRequest(
        call_id="call_a",
        plugin="bash",
        tool="run",
        args_preview="ls -la",
        session_key="sess_1",
        reason="first use",
    )
    await store.insert(req)

    pending = await store.pending()
    assert len(pending) == 1
    assert pending[0].call_id == "call_a"
    assert pending[0].decision is None


@pytest.mark.asyncio
async def test_store_decide_round_trip() -> None:
    store = ApprovalStore()
    req = ApprovalRequest(
        call_id="call_b",
        plugin="bash",
        tool="run",
        args_preview="echo hi",
        session_key="sess_2",
        reason="manual",
    )
    await store.insert(req)
    assert await store.decide("call_b", ApprovalDecision.ALLOW) is True

    record = await store.get("call_b")
    assert record is not None
    assert record.decision == ApprovalDecision.ALLOW
    assert record.decided_at is not None

    # Already decided rows are not updated again.
    assert await store.decide("call_b", ApprovalDecision.DENY) is False
    record = await store.get("call_b")
    assert record is not None
    assert record.decision == ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_store_decide_unknown_returns_false() -> None:
    store = ApprovalStore()
    assert await store.decide("does-not-exist", ApprovalDecision.ALLOW) is False


@pytest.mark.asyncio
async def test_has_prior_approval_for_session() -> None:
    store = ApprovalStore()
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False

    await store.insert(
        ApprovalRequest(
            call_id="call_c",
            plugin="bash",
            tool="run",
            args_preview="...",
            session_key="sess_x",
            reason="...",
        )
    )
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False
    await store.decide("call_c", ApprovalDecision.DENY)
    assert await store.has_prior_approval_for_session("sess_x", "bash") is False
    await store.insert(
        ApprovalRequest(
            call_id="call_d",
            plugin="bash",
            tool="run",
            args_preview="...",
            session_key="sess_x",
            reason="...",
        )
    )
    await store.decide("call_d", ApprovalDecision.ALLOW)
    assert await store.has_prior_approval_for_session("sess_x", "bash") is True


@pytest.mark.asyncio
async def test_queue_enqueue_and_wait_resolves_on_decide() -> None:
    queue = ApprovalQueue()
    call_id = queue.new_call_id()
    req = ApprovalRequest(
        call_id=call_id,
        plugin="bash",
        tool="run",
        args_preview="ls",
        session_key="sess_q",
        reason="reason",
    )

    waiter = asyncio.create_task(queue.enqueue_and_wait(req, timeout=2.0))
    # Wait briefly to ensure the waiter has subscribed.
    await asyncio.sleep(0.01)
    assert await queue.decide(call_id, ApprovalDecision.ALLOW) is True

    decision = await waiter
    assert decision == ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_queue_wait_fast_path_for_already_decided() -> None:
    queue = ApprovalQueue()
    call_id = queue.new_call_id()
    req = ApprovalRequest(
        call_id=call_id,
        plugin="bash",
        tool="run",
        args_preview="ls",
        session_key="sess_fast",
        reason="reason",
    )
    await queue.enqueue(req)
    await queue.decide(call_id, ApprovalDecision.DENY)
    decision = await queue.wait(call_id, timeout=1.0)
    assert decision == ApprovalDecision.DENY


@pytest.mark.asyncio
async def test_queue_wait_timeout() -> None:
    queue = ApprovalQueue()
    call_id = queue.new_call_id()
    await queue.enqueue(
        ApprovalRequest(
            call_id=call_id,
            plugin="bash",
            tool="run",
            args_preview="ls",
            session_key="sess_to",
            reason="reason",
        )
    )
    with pytest.raises(asyncio.TimeoutError):
        await queue.wait(call_id, timeout=0.05)


@pytest.mark.asyncio
async def test_is_first_use_policy() -> None:
    queue = ApprovalQueue()
    assert await queue.is_first_use("sess_fu", "bash") is True

    call_id = queue.new_call_id()
    await queue.enqueue(
        ApprovalRequest(
            call_id=call_id,
            plugin="bash",
            tool="run",
            args_preview="ls",
            session_key="sess_fu",
            reason="reason",
        )
    )
    await queue.decide(call_id, ApprovalDecision.ALLOW)
    assert await queue.is_first_use("sess_fu", "bash") is False
