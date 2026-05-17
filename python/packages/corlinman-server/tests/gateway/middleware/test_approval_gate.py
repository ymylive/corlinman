"""End-to-end tests for :class:`ApprovalGate`.

Mirrors the Rust ``approval.rs`` ``check`` / ``resolve`` ``#[tokio::test]``
table tests: AUTO returns approved without persisting, DENY records a
decided row, PROMPT honours a session-key whitelist, PROMPT times out
and persists a timeout outcome, and operator-driven ``resolve`` wakes a
parked ``check``.
"""

from __future__ import annotations

import asyncio

import pytest

from corlinman_providers.plugins.approval import ApprovalStore
from corlinman_server.gateway.middleware import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalMode,
    ApprovalRule,
)


@pytest.mark.asyncio
async def test_check_auto_returns_approved_without_persisting() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[ApprovalRule(plugin="file-ops", mode=ApprovalMode.AUTO)],
        store=store,
        default_timeout_seconds=0.2,
    )
    decision, _ = await gate.check(
        session_key="s1", plugin="file-ops", tool="read", args_json=b"{}"
    )
    assert decision is ApprovalDecision.APPROVED
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_check_deny_records_decided_row() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[ApprovalRule(plugin="shell", mode=ApprovalMode.DENY)],
        store=store,
        default_timeout_seconds=0.2,
    )
    decision, cid = await gate.check(
        session_key="s1", plugin="shell", tool="exec", args_json=b"{}"
    )
    assert decision is ApprovalDecision.DENIED
    record = await store.get(cid)
    assert record is not None
    assert record.decision is not None
    assert record.decision.value == "deny"


@pytest.mark.asyncio
async def test_check_prompt_times_out_and_persists_timeout() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[ApprovalRule(plugin="shell", mode=ApprovalMode.PROMPT)],
        store=store,
        default_timeout_seconds=0.05,
    )
    decision, cid = await gate.check(
        session_key="s1", plugin="shell", tool="exec", args_json=b"{}"
    )
    assert decision is ApprovalDecision.TIMEOUT
    # Row stays in the DB even though it timed out — admin UI needs to see it.
    record = await store.get(cid)
    assert record is not None


@pytest.mark.asyncio
async def test_check_prompt_with_whitelist_approves_without_row() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[
            ApprovalRule(
                plugin="shell",
                mode=ApprovalMode.PROMPT,
                allow_session_keys=("s1",),
            )
        ],
        store=store,
        default_timeout_seconds=0.2,
    )
    decision, _ = await gate.check(
        session_key="s1", plugin="shell", tool="exec", args_json=b"{}"
    )
    assert decision is ApprovalDecision.APPROVED
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_resolve_wakes_pending_check() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[ApprovalRule(plugin="shell", mode=ApprovalMode.PROMPT)],
        store=store,
        default_timeout_seconds=5.0,
    )

    async def call() -> tuple[ApprovalDecision, str]:
        return await gate.check(
            session_key="s1", plugin="shell", tool="exec", args_json=b"{}"
        )

    task = asyncio.create_task(call())

    # Spin briefly until the queued row lands.
    cid: str | None = None
    for _ in range(200):
        pending = await store.pending()
        if pending:
            cid = pending[0].call_id
            break
        await asyncio.sleep(0.01)
    assert cid is not None, "approval row never appeared"

    await gate.resolve(cid, ApprovalDecision.APPROVED)
    decision, _ = await task
    assert decision is ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_resolve_unknown_call_id_raises_lookup_error() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(
        rules=[ApprovalRule(plugin="shell", mode=ApprovalMode.PROMPT)],
        store=store,
    )
    with pytest.raises(LookupError):
        await gate.resolve("call_does_not_exist", ApprovalDecision.APPROVED)


@pytest.mark.asyncio
async def test_check_no_match_defaults_to_approved() -> None:
    store = ApprovalStore()
    gate = ApprovalGate(rules=[], store=store)
    decision, _ = await gate.check(
        session_key="s1", plugin="anything", tool="x", args_json=b"{}"
    )
    assert decision is ApprovalDecision.APPROVED
