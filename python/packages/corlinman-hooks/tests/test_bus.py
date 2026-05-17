"""Port of ``rust/crates/corlinman-hooks/tests/bus.rs``.

Covers:
    (a) each event variant round-trips through the bus,
    (b) Critical subscribers observe an event before Normal/Low do,
    (c) flipping the cancel token stops further emits,
    (d) a dropped subscriber does not break emits for the others,
    (e) approval / rate-limit / telemetry variants round-trip via JSON
        with the wire-stable ``kind`` discriminant and the
        :meth:`HookEvent.session_key` accessor reports the expected
        scoping.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from corlinman_hooks import (
    HookBus,
    HookCancelledError,
    HookEvent,
    HookPriority,
    Lagged,
)


def _all_event_samples() -> list:
    """Mirror of ``all_event_samples`` in the Rust test module."""
    return [
        HookEvent.MessageReceived(
            channel="qq",
            session_key_="s1",
            content="hi",
            metadata={"from": "u1"},
            user_id=None,
        ),
        HookEvent.MessageSent(
            channel="qq",
            session_key_="s1",
            content="hello",
            success=True,
            user_id=None,
        ),
        HookEvent.MessageTranscribed(
            session_key_="s1",
            transcript="spoken text",
            media_path="/tmp/a.ogg",
            media_type="audio/ogg",
            user_id=None,
        ),
        HookEvent.MessagePreprocessed(
            session_key_="s1",
            transcript="cleaned",
            is_group=True,
            group_id="g42",
            user_id=None,
        ),
        HookEvent.SessionPatch(
            session_key_="s1",
            patch={"foo": "bar"},
            user_id=None,
        ),
        HookEvent.AgentBootstrap(
            workspace_dir="/ws",
            session_key_="s1",
            files=["a.md", "b.md"],
        ),
        HookEvent.GatewayStartup(version="0.1.0"),
        HookEvent.ConfigChanged(
            section="channels.qq",
            old={"enabled": False},
            new={"enabled": True},
        ),
        HookEvent.ApprovalRequested(
            id="a1",
            session_key_="s1",
            plugin="shell",
            tool="exec",
            args_preview="{}",
            timeout_at_ms=0,
            user_id=None,
        ),
        HookEvent.ApprovalDecided(
            id="a1",
            decision="allow",
            decider="root",
            decided_at_ms=0,
            tenant_id=None,
            user_id=None,
        ),
        HookEvent.RateLimitTriggered(
            session_key_="s1",
            limit_type="channel_qq",
            retry_after_ms=0,
            user_id=None,
        ),
        HookEvent.Telemetry(
            node_id="ios-demo",
            metric="battery.level",
            value=0.87,
            tags={"build": "dev"},
        ),
        HookEvent.EngineRunCompleted(
            run_id="r1",
            proposals_generated=3,
            duration_ms=420,
        ),
        HookEvent.EngineRunFailed(
            run_id="r2",
            error_kind="timeout",
            exit_code=None,
        ),
    ]


async def test_each_event_round_trips() -> None:
    bus = HookBus(capacity=64)
    sub = bus.subscribe(HookPriority.NORMAL)

    for ev in _all_event_samples():
        await bus.emit(ev)
        got = await sub.recv()
        assert got.kind() == ev.kind(), f"kind mismatch for {ev.kind()!r}"


async def test_critical_observes_before_normal_and_low() -> None:
    """Critical subscribers must observe events strictly before Normal /
    Low ones. Runs on the default single-threaded asyncio runtime —
    the ordering guarantee is cooperative (the ``await sleep(0)``
    between tiers lets pending receivers drain before the next tier
    is published).
    """
    bus = HookBus(capacity=64)
    log: list[tuple[str, int]] = []

    critical = bus.subscribe(HookPriority.CRITICAL)
    normal = bus.subscribe(HookPriority.NORMAL)
    low = bus.subscribe(HookPriority.LOW)

    async def drain(sub, label: str) -> None:
        for i in range(5):
            await sub.recv()
            log.append((label, i))

    crit_task = asyncio.create_task(drain(critical, "critical"))
    norm_task = asyncio.create_task(drain(normal, "normal"))
    low_task = asyncio.create_task(drain(low, "low"))

    for i in range(5):
        await bus.emit(HookEvent.GatewayStartup(version=f"v{i}"))

    await asyncio.gather(crit_task, norm_task, low_task)

    for i in range(5):
        crit_pos = log.index(("critical", i))
        norm_pos = log.index(("normal", i))
        low_pos = log.index(("low", i))
        assert crit_pos < norm_pos, (
            f"event {i}: critical ({crit_pos}) should precede normal ({norm_pos}): {log}"
        )
        assert norm_pos < low_pos, (
            f"event {i}: normal ({norm_pos}) should precede low ({low_pos}): {log}"
        )


async def test_cancel_propagates_and_stops_downstream_emits() -> None:
    bus = HookBus(capacity=64)
    sub = bus.subscribe(HookPriority.NORMAL)
    cancel = bus.cancel_token()

    await bus.emit(HookEvent.GatewayStartup(version="pre"))
    got = await sub.recv()
    assert got.kind() == "gateway_startup"

    cancel.cancel()

    with pytest.raises(HookCancelledError):
        await bus.emit(HookEvent.GatewayStartup(version="post"))

    # Subscriber should not receive the second event. Use a short timeout
    # because `recv` would otherwise hang waiting for the next emit.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.recv(), timeout=0.05)


async def test_approval_requested_round_trips_and_exposes_session_key() -> None:
    bus = HookBus(capacity=16)
    sub = bus.subscribe(HookPriority.NORMAL)

    ev = HookEvent.ApprovalRequested(
        id="req-1",
        session_key_="qq:group:123:u42",
        plugin="shell",
        tool="exec",
        args_preview='{"cmd":"ls"}',
        timeout_at_ms=1_700_000_000_000,
        user_id=None,
    )
    await bus.emit(ev)
    got = await sub.recv()
    assert got.kind() == "approval_requested"
    assert got.session_key() == "qq:group:123:u42"

    # JSON round-trip so the admin UI / Rust bridge wire contract is pinned.
    raw = ev.to_json()
    assert '"kind":"ApprovalRequested"' in raw
    back = HookEvent.from_json(raw)
    assert back.kind() == "approval_requested"


async def test_approval_decided_round_trips_and_is_session_scoped_none() -> None:
    bus = HookBus(capacity=16)
    sub = bus.subscribe(HookPriority.NORMAL)

    ev = HookEvent.ApprovalDecided(
        id="req-1",
        decision="allow",
        decider="admin",
        decided_at_ms=1_700_000_000_500,
        tenant_id=None,
        user_id=None,
    )
    await bus.emit(ev)
    got = await sub.recv()
    assert got.kind() == "approval_decided"
    # Decisions are not session-scoped on the bus (the `id` links back).
    assert got.session_key() is None

    raw = ev.to_json()
    back = HookEvent.from_json(raw)
    assert isinstance(back, HookEvent.ApprovalDecided)
    assert back.decision == "allow"
    assert back.decider == "admin"


async def test_rate_limit_triggered_round_trips() -> None:
    bus = HookBus(capacity=16)
    sub = bus.subscribe(HookPriority.NORMAL)

    ev = HookEvent.RateLimitTriggered(
        session_key_="qq:group:999:u7",
        limit_type="channel_qq",
        retry_after_ms=500,
        user_id=None,
    )
    await bus.emit(ev)
    got = await sub.recv()
    assert got.kind() == "rate_limit_triggered"
    assert got.session_key() == "qq:group:999:u7"

    raw = ev.to_json()
    back = HookEvent.from_json(raw)
    assert back.kind() == "rate_limit_triggered"


async def test_telemetry_round_trips_with_stable_tag_order() -> None:
    bus = HookBus(capacity=16)
    sub = bus.subscribe(HookPriority.NORMAL)

    ev = HookEvent.Telemetry(
        node_id="ios-demo",
        metric="battery.level",
        value=0.42,
        # Insert in non-sorted order; the dataclass sorts at serialization time
        # to mirror Rust's BTreeMap stable-key-order guarantee.
        tags={"region": "cn", "build": "dev"},
    )
    await bus.emit(ev)
    got = await sub.recv()
    assert got.kind() == "telemetry"
    assert got.session_key() is None

    raw = ev.to_json()
    build_at = raw.find("build")
    region_at = raw.find("region")
    assert build_at != -1 and region_at != -1
    assert build_at < region_at, (
        f"telemetry tags must serialize in lexicographic key order: {raw}"
    )
    back = HookEvent.from_json(raw)
    assert back.kind() == "telemetry"


async def test_dropped_subscriber_does_not_break_emits() -> None:
    bus = HookBus(capacity=64)
    kept = bus.subscribe(HookPriority.NORMAL)
    doomed = bus.subscribe(HookPriority.NORMAL)
    # Drop the doomed sub explicitly. CPython refcounting collects it
    # immediately; the bus's weak-ref bookkeeping prunes it on the
    # next emit.
    del doomed

    for i in range(3):
        await bus.emit(HookEvent.GatewayStartup(version=f"v{i}"))

    for _ in range(3):
        got = await kept.recv()
        assert got.kind() == "gateway_startup"


# ---------------------------------------------------------------------------
# Extra coverage that the Python port adds on top of the Rust suite.
# These exercise asyncio-specific edges (lag surfacing + nonblocking emit).
# ---------------------------------------------------------------------------


async def test_slow_subscriber_surfaces_lagged_then_resumes() -> None:
    """When a subscriber's buffer overflows the bus's capacity, the
    next ``recv`` must surface a :class:`Lagged` exception carrying
    the number of dropped events, then resume normal delivery starting
    with the *oldest still-buffered* event.
    """
    bus = HookBus(capacity=2)
    sub = bus.subscribe(HookPriority.NORMAL)

    # Emit 5 events without draining — only the latest 2 (capacity)
    # remain in the buffer; the first 3 are dropped and counted as lag.
    for i in range(5):
        await bus.emit(HookEvent.GatewayStartup(version=f"v{i}"))

    with pytest.raises(Lagged) as excinfo:
        await sub.recv()
    assert excinfo.value.count == 3

    # After the lag is reported, the next recv returns the oldest event
    # still in the buffer (v3, since v0/v1/v2 were dropped).
    nxt = await sub.recv()
    assert isinstance(nxt, HookEvent.GatewayStartup)
    assert nxt.version == "v3"


async def test_emit_nonblocking_fans_out_without_yielding() -> None:
    bus = HookBus(capacity=8)
    crit = bus.subscribe(HookPriority.CRITICAL)
    norm = bus.subscribe(HookPriority.NORMAL)
    low = bus.subscribe(HookPriority.LOW)

    bus.emit_nonblocking(HookEvent.GatewayStartup(version="sync"))

    for sub in (crit, norm, low):
        got = await sub.recv()
        assert isinstance(got, HookEvent.GatewayStartup)
        assert got.version == "sync"


async def test_emit_nonblocking_respects_cancel() -> None:
    bus = HookBus(capacity=4)
    sub = bus.subscribe(HookPriority.NORMAL)
    bus.cancel_token().cancel()

    bus.emit_nonblocking(HookEvent.GatewayStartup(version="post-cancel"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.recv(), timeout=0.05)


def test_priority_ordered_matches_rust() -> None:
    assert HookPriority.ordered() == (
        HookPriority.CRITICAL,
        HookPriority.NORMAL,
        HookPriority.LOW,
    )


def test_event_from_dict_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown hook event kind"):
        HookEvent.from_dict({"kind": "NotARealVariant"})


def test_event_from_dict_rejects_missing_kind() -> None:
    with pytest.raises(ValueError, match="missing or non-string 'kind' field"):
        HookEvent.from_dict({"version": "x"})


def test_subagent_events_session_key_picks_child() -> None:
    """Subagent events surface the *child's* session_key (when present),
    matching the Rust ``session_key()`` matcher.
    """
    spawned = HookEvent.SubagentSpawned(
        parent_session_key="parent-s",
        child_session_key="child-s",
        child_agent_id="agent-1",
        agent_card="{}",
        depth=1,
        parent_trace_id="trace-1",
        tenant_id="default",
    )
    assert spawned.session_key() == "child-s"

    capped = HookEvent.SubagentDepthCapped(
        parent_session_key="parent-s",
        attempted_depth=4,
        reason="depth_capped",
        parent_trace_id="trace-1",
        tenant_id="default",
    )
    # Pre-spawn rejections have no child session yet, so report the parent's.
    assert capped.session_key() == "parent-s"


def test_event_to_dict_skips_none_optionals() -> None:
    ev = HookEvent.MessageReceived(
        channel="qq",
        session_key_="s1",
        content="hi",
        metadata={},
        user_id=None,
    )
    d = ev.to_dict()
    # ``user_id`` is ``None`` so the serializer must skip it to match
    # the Rust ``skip_serializing_if = "Option::is_none"`` behaviour.
    assert "user_id" not in d
    # And the dataclass-internal ``session_key_`` attribute is mapped
    # back to the canonical ``session_key`` JSON key.
    assert "session_key_" not in d
    assert d["session_key"] == "s1"
    # Round-trip back through the discriminant dispatcher works too.
    back = HookEvent.from_json(json.dumps(d))
    assert isinstance(back, HookEvent.MessageReceived)
    assert back.session_key_ == "s1"
    assert back.user_id is None
