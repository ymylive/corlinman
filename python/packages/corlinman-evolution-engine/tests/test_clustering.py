"""Tests for ``cluster_signals``."""

from __future__ import annotations

import pytest
from corlinman_evolution_engine.clustering import cluster_signals
from corlinman_evolution_engine.store import SignalRow


def _signal(
    *,
    sid: int,
    event_kind: str,
    target: str | None,
    trace_id: str | None = None,
) -> SignalRow:
    return SignalRow(
        id=sid,
        event_kind=event_kind,
        target=target,
        severity="warn",
        payload={},
        trace_id=trace_id,
        session_id=None,
        observed_at=sid * 1_000,
    )


def test_three_identical_signals_form_one_cluster() -> None:
    signals = [
        _signal(sid=1, event_kind="tool.call.failed", target="web_search"),
        _signal(sid=2, event_kind="tool.call.failed", target="web_search"),
        _signal(sid=3, event_kind="tool.call.failed", target="web_search"),
    ]

    clusters = cluster_signals(signals, min_cluster_size=3)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.size == 3
    assert cluster.event_kind == "tool.call.failed"
    assert cluster.target == "web_search"
    assert cluster.signal_ids == [1, 2, 3]


def test_two_distinct_targets_yield_no_clusters_at_threshold_three() -> None:
    signals = [
        _signal(sid=1, event_kind="tool.call.failed", target="web_search"),
        _signal(sid=2, event_kind="tool.call.failed", target="github_search"),
    ]

    clusters = cluster_signals(signals, min_cluster_size=3)

    assert clusters == []


def test_below_threshold_groups_are_dropped() -> None:
    signals = [
        _signal(sid=1, event_kind="tool.call.failed", target="web_search"),
        _signal(sid=2, event_kind="tool.call.failed", target="web_search"),
        _signal(sid=3, event_kind="approval.rejected", target="merge_chunks:1,2"),
    ]

    clusters = cluster_signals(signals, min_cluster_size=3)

    assert clusters == []


def test_clusters_returned_in_size_descending_order() -> None:
    signals = [
        # 4 of kind A
        _signal(sid=1, event_kind="A", target="t1"),
        _signal(sid=2, event_kind="A", target="t1"),
        _signal(sid=3, event_kind="A", target="t1"),
        _signal(sid=4, event_kind="A", target="t1"),
        # 3 of kind B
        _signal(sid=5, event_kind="B", target="t2"),
        _signal(sid=6, event_kind="B", target="t2"),
        _signal(sid=7, event_kind="B", target="t2"),
    ]

    clusters = cluster_signals(signals, min_cluster_size=3)

    assert [c.size for c in clusters] == [4, 3]
    assert clusters[0].event_kind == "A"
    assert clusters[1].event_kind == "B"


def test_trace_ids_are_dedup_in_insertion_order() -> None:
    signals = [
        _signal(sid=1, event_kind="A", target="t", trace_id="trace-x"),
        _signal(sid=2, event_kind="A", target="t", trace_id="trace-y"),
        _signal(sid=3, event_kind="A", target="t", trace_id="trace-x"),
        _signal(sid=4, event_kind="A", target="t", trace_id=None),
    ]

    clusters = cluster_signals(signals, min_cluster_size=3)

    assert len(clusters) == 1
    assert clusters[0].trace_ids == ["trace-x", "trace-y"]


def test_min_cluster_size_zero_rejected() -> None:
    with pytest.raises(ValueError):
        cluster_signals([], min_cluster_size=0)


def test_signals_grouping_treats_none_target_as_distinct_from_empty_string() -> None:
    signals = [
        _signal(sid=1, event_kind="A", target=None),
        _signal(sid=2, event_kind="A", target=None),
        _signal(sid=3, event_kind="A", target=""),
    ]

    clusters = cluster_signals(signals, min_cluster_size=2)

    assert len(clusters) == 1
    assert clusters[0].target is None
    assert clusters[0].size == 2
