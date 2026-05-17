"""Tests for the metrics primitives.

Ports ``rust/crates/corlinman-auto-rollback/src/metrics.rs::tests``.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from corlinman_auto_rollback.config import AutoRollbackThresholds
from corlinman_auto_rollback.metrics import (
    MetricSnapshot,
    breaches_threshold,
    capture_snapshot,
    compute_delta,
    watched_event_kinds,
)
from corlinman_evolution_store import EvolutionKind, EvolutionStore


async def _seed_signal(
    store: EvolutionStore,
    *,
    event_kind: str,
    target: str,
    severity: str,
    observed_at: int,
) -> None:
    """Insert one signal row directly via SQL — bypasses the repo so
    tests can construct edge cases the typed API would reject."""
    await store.conn.execute(
        """INSERT INTO evolution_signals
             (event_kind, target, severity, payload_json, observed_at)
           VALUES (?, ?, ?, '{}', ?)""",
        (event_kind, target, severity, observed_at),
    )
    await store.conn.commit()


def _snap(target: str, counts: Iterable[tuple[str, int]]) -> MetricSnapshot:
    return MetricSnapshot(
        target=target,
        captured_at_ms=0,
        window_secs=1_800,
        counts=dict(counts),
    )


def _thresholds(min_baseline: int, pct: float) -> AutoRollbackThresholds:
    return AutoRollbackThresholds(
        default_err_rate_delta_pct=pct,
        default_p95_latency_delta_pct=25.0,
        signal_window_secs=1_800,
        min_baseline_signals=min_baseline,
    )


@pytest.mark.asyncio
async def test_capture_snapshot_empty_db(store: EvolutionStore) -> None:
    snap = await capture_snapshot(
        store.conn,
        "delete_chunk:1",
        ("tool.call.failed", "search.recall.dropped"),
        1_800,
        10_000_000,
    )
    assert snap.target == "delete_chunk:1"
    assert snap.window_secs == 1_800
    assert snap.captured_at_ms == 10_000_000
    assert snap.counts.get("tool.call.failed") == 0
    assert snap.counts.get("search.recall.dropped") == 0


@pytest.mark.asyncio
async def test_capture_snapshot_filters_by_target_and_window(
    store: EvolutionStore,
) -> None:
    now = 10_000_000
    # In-window, matching target — should count.
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="delete_chunk:1",
        severity="error",
        observed_at=now - 60_000,
    )
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="delete_chunk:1",
        severity="warn",
        observed_at=now - 1_000,
    )
    # In-window, *different* target — must be excluded.
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="delete_chunk:99",
        severity="error",
        observed_at=now - 1_000,
    )
    # Matching target, but observed_at older than the window.
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="delete_chunk:1",
        severity="error",
        observed_at=now - 10_000_000,
    )

    snap = await capture_snapshot(
        store.conn, "delete_chunk:1", ("tool.call.failed",), 1_800, now
    )
    assert snap.counts["tool.call.failed"] == 2


@pytest.mark.asyncio
async def test_capture_snapshot_filters_severity(store: EvolutionStore) -> None:
    now = 10_000_000
    # info-severity is noise — must not show up in regression counts.
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="t",
        severity="info",
        observed_at=now - 1_000,
    )
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="t",
        severity="warn",
        observed_at=now - 1_000,
    )
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="t",
        severity="error",
        observed_at=now - 1_000,
    )
    snap = await capture_snapshot(
        store.conn, "t", ("tool.call.failed",), 1_800, now
    )
    assert snap.counts["tool.call.failed"] == 2


@pytest.mark.asyncio
async def test_capture_snapshot_empty_event_kinds_returns_empty_counts(
    store: EvolutionStore,
) -> None:
    await _seed_signal(
        store,
        event_kind="tool.call.failed",
        target="t",
        severity="error",
        observed_at=1_000,
    )
    snap = await capture_snapshot(store.conn, "t", (), 1_800, 10_000_000)
    assert snap.counts == {}
    assert snap.target == "t"
    assert snap.window_secs == 1_800


def test_compute_delta_zero_baseline() -> None:
    baseline = _snap("t", [("tool.call.failed", 0)])
    current = _snap("t", [("tool.call.failed", 5)])
    d = compute_delta(baseline, current)
    assert d.baseline_total == 0
    assert d.current_total == 5
    # denom floored at 1 -> no NaN/Inf even when baseline is zero.
    assert d.rel_pct == 500.0


def test_compute_delta_proportional() -> None:
    baseline = _snap("t", [("tool.call.failed", 4)])
    current = _snap("t", [("tool.call.failed", 6)])
    d = compute_delta(baseline, current)
    assert d.baseline_total == 4
    assert d.current_total == 6
    assert d.abs_delta == 2
    assert abs(d.rel_pct - 50.0) < 1e-9


def test_breaches_threshold_quiet_target_no_alarm() -> None:
    baseline = _snap("t", [("tool.call.failed", 0)])
    current = _snap("t", [("tool.call.failed", 100)])
    d = compute_delta(baseline, current)
    t = _thresholds(5, 50.0)
    # quiet-target guard kicks in — no rollback even though rel_pct is huge.
    assert breaches_threshold(d, t) is None


def test_breaches_threshold_loud_target_above_pct() -> None:
    baseline = _snap("t", [("tool.call.failed", 10)])
    current = _snap("t", [("tool.call.failed", 20)])
    d = compute_delta(baseline, current)
    t = _thresholds(5, 50.0)
    reason = breaches_threshold(d, t)
    assert reason is not None
    assert "10" in reason
    assert "20" in reason
    assert "breaches threshold" in reason


def test_watched_event_kinds_memory_op_present() -> None:
    kinds = watched_event_kinds(EvolutionKind.MEMORY_OP)
    assert "tool.call.failed" in kinds
    assert "search.recall.dropped" in kinds


def test_watched_event_kinds_unknown_returns_empty() -> None:
    for k in [
        EvolutionKind.TAG_REBALANCE,
        EvolutionKind.RETRY_TUNING,
        EvolutionKind.AGENT_CARD,
        EvolutionKind.SKILL_UPDATE,
        EvolutionKind.PROMPT_TEMPLATE,
        EvolutionKind.TOOL_POLICY,
        EvolutionKind.NEW_SKILL,
    ]:
        assert watched_event_kinds(k) == (), f"{k} should be empty"


def test_metric_snapshot_roundtrip() -> None:
    snap = _snap("t", [("a", 1), ("b", 2)])
    decoded = MetricSnapshot.from_dict(snap.to_dict())
    assert decoded == snap


def test_metric_snapshot_from_dict_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        MetricSnapshot.from_dict("totally-not-a-snapshot")


def test_metric_snapshot_from_dict_rejects_missing_field() -> None:
    with pytest.raises(ValueError):
        MetricSnapshot.from_dict({"target": "t", "captured_at_ms": 0})
