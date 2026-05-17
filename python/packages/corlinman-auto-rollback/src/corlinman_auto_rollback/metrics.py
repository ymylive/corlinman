"""Metric snapshot + delta computation against ``evolution_signals``.

Ported 1:1 from ``rust/crates/corlinman-auto-rollback/src/metrics.rs``.

At apply time the applier writes a :class:`MetricSnapshot` JSON into
``evolution_history.metrics_baseline``. At monitor time we take a
fresh :class:`MetricSnapshot` over the same window length and feed
both into :func:`compute_delta` -> :func:`breaches_threshold`.

Why ``evolution_signals`` and not Prometheus: the monitor lives in the
same process tree as the engine and we already store severity-typed
event rows there. No scrape, no second time-series store.

Dict iteration order is insertion-ordered (Python 3.7+), and the
:func:`capture_snapshot` SQL loop walks ``event_kinds`` in input
order so two snapshots taken back-to-back diff cleanly. The Rust port
uses ``BTreeMap`` for the same stability — sorting keys in
:func:`compute_delta` keeps the union deterministic.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import aiosqlite
from corlinman_evolution_store import EvolutionKind

from corlinman_auto_rollback.config import AutoRollbackThresholds


@dataclass
class MetricSnapshot:
    """Per-event-kind signal counts captured over a sliding window.

    Written verbatim into ``evolution_history.metrics_baseline`` as
    JSON at apply time; the monitor takes a fresh one and computes a
    delta against it.

    ``counts`` is ordered by insertion (and re-sorted in
    :meth:`to_dict` for stable serialization) so two snapshots taken
    back-to-back diff cleanly.
    """

    target: str
    captured_at_ms: int
    window_secs: int
    counts: dict[str, int] = field(default_factory=dict)
    """``event_kind`` -> count over the window. Empty when the slice
    of watched kinds was empty (kind not yet wired for AutoRollback)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict. Sorts ``counts`` keys
        so the on-disk bytes are stable regardless of insertion order."""
        return {
            "target": self.target,
            "captured_at_ms": self.captured_at_ms,
            "window_secs": self.window_secs,
            "counts": {k: int(self.counts[k]) for k in sorted(self.counts)},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, raw: Any) -> MetricSnapshot:
        """Strict decode — matches the Rust ``serde_json::from_value``
        path. ``raw`` not being an object, or any of the four required
        fields being absent / wrong-typed, raises :class:`ValueError`
        so the monitor's fail-safe "skip on bad baseline" branch fires
        instead of guessing."""
        if not isinstance(raw, dict):
            raise ValueError(
                f"MetricSnapshot.from_dict: expected JSON object, got {type(raw).__name__}"
            )
        try:
            target = str(raw["target"])
            captured_at_ms = int(raw["captured_at_ms"])
            window_secs = int(raw["window_secs"])
            counts_raw = raw["counts"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"MetricSnapshot.from_dict: missing/invalid field: {exc}") from exc
        if not isinstance(counts_raw, dict):
            raise ValueError(
                f"MetricSnapshot.from_dict: 'counts' must be object, got {type(counts_raw).__name__}"
            )
        counts: dict[str, int] = {}
        for key, value in counts_raw.items():
            counts[str(key)] = int(value)
        return cls(
            target=target,
            captured_at_ms=captured_at_ms,
            window_secs=window_secs,
            counts=counts,
        )


@dataclass
class KindDelta:
    """Per-event-kind portion of :class:`MetricDelta`."""

    baseline: int
    current: int
    abs_delta: int
    rel_pct: float


@dataclass
class MetricDelta:
    """Computed delta between two :class:`MetricSnapshot` instances.

    ``rel_pct`` denominator is floored at 1 to keep quiet targets
    NaN-free (matches the Rust ``b.max(1)`` floor).
    """

    target: str
    baseline_total: int
    current_total: int
    abs_delta: int
    rel_pct: float
    per_event_kind: dict[str, KindDelta] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "baseline_total": self.baseline_total,
            "current_total": self.current_total,
            "abs_delta": self.abs_delta,
            "rel_pct": self.rel_pct,
            "per_event_kind": {k: asdict(v) for k, v in sorted(self.per_event_kind.items())},
        }


def watched_event_kinds(kind: EvolutionKind) -> tuple[str, ...]:
    """Which ``evolution_signals.event_kind`` values count as a
    regression signal for a given :class:`EvolutionKind`.

    Targeted to start: ``memory_op`` only. New kinds extend the
    mapping as their handlers land. An empty tuple means "monitor
    sees no signals" so we never auto-rollback a kind we don't yet
    have a signal contract for — safer than guessing.
    """
    if kind is EvolutionKind.MEMORY_OP:
        return ("tool.call.failed", "search.recall.dropped")
    return ()


async def capture_snapshot(
    conn: aiosqlite.Connection,
    target: str,
    event_kinds: tuple[str, ...] | list[str],
    window_secs: int,
    now_ms: int,
) -> MetricSnapshot:
    """Count signals over a sliding window per ``event_kind``,
    filtered to ``warn`` / ``error`` severity. ``info`` is noise for
    regression purposes.

    Empty ``event_kinds`` -> empty ``counts`` dict; the rest of the
    snapshot is still populated so the applier can persist a stable
    baseline shape and the monitor can detect "no whitelist for this
    kind" without a special case.
    """
    since_ms = now_ms - window_secs * 1_000
    counts: dict[str, int] = {}

    # One parameterised query per event_kind. The set is small
    # (memory_op = 2 today, max ~10 per kind) so per-kind round-trips
    # are cheaper than building a dynamic IN clause.
    for kind_str in event_kinds:
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM evolution_signals
               WHERE target = ?
                 AND event_kind = ?
                 AND observed_at >= ?
                 AND severity IN ('warn', 'error')""",
            (target, kind_str, since_ms),
        )
        row = await cursor.fetchone()
        await cursor.close()
        count = 0 if row is None or row[0] is None else int(row[0])
        counts[kind_str] = count

    return MetricSnapshot(
        target=target,
        captured_at_ms=now_ms,
        window_secs=window_secs,
        counts=counts,
    )


def compute_delta(baseline: MetricSnapshot, current: MetricSnapshot) -> MetricDelta:
    """Pure diff between two snapshots.

    The union of ``event_kinds`` across both inputs guarantees a
    stable shape even when the whitelist changes between
    baseline-capture and current-capture (e.g. config edit
    mid-grace-window).
    """
    all_kinds: set[str] = set(baseline.counts.keys()) | set(current.counts.keys())

    per_event_kind: dict[str, KindDelta] = {}
    baseline_total = 0
    current_total = 0
    for kind_str in sorted(all_kinds):
        b = int(baseline.counts.get(kind_str, 0))
        c = int(current.counts.get(kind_str, 0))
        baseline_total += b
        current_total += c
        abs_delta = c - b
        denom = float(max(b, 1))
        rel_pct = (abs_delta / denom) * 100.0
        per_event_kind[kind_str] = KindDelta(
            baseline=b,
            current=c,
            abs_delta=abs_delta,
            rel_pct=rel_pct,
        )

    abs_delta = current_total - baseline_total
    denom = float(max(baseline_total, 1))
    rel_pct = (abs_delta / denom) * 100.0

    return MetricDelta(
        # Both snapshots target the same proposal; pick baseline's.
        target=baseline.target,
        baseline_total=baseline_total,
        current_total=current_total,
        abs_delta=abs_delta,
        rel_pct=rel_pct,
        per_event_kind=per_event_kind,
    )


def breaches_threshold(
    delta: MetricDelta,
    thresholds: AutoRollbackThresholds,
) -> str | None:
    """Decide whether a delta breaches the configured rollback
    threshold.

    Returns a human-readable reason string when both:

    * ``baseline_total >= min_baseline_signals`` — quiet-target guard;
      a target that emitted near-zero pre-apply doesn't deserve a
      rollback on the first post-apply spike, and
    * ``rel_pct >= default_err_rate_delta_pct``.

    Returns ``None`` otherwise.

    ``default_p95_latency_delta_pct`` is intentionally not consulted
    here — W1-B's memory_op path doesn't emit latency-bucketed
    signals, so folding it into the breach test would be lying about
    what we're measuring. Future kinds that emit latency signals get
    their own branch.
    """
    if delta.baseline_total < thresholds.min_baseline_signals:
        return None
    if delta.rel_pct < thresholds.default_err_rate_delta_pct:
        return None
    sign = "+" if delta.abs_delta >= 0 else ""
    return (
        f"err_signal_count: {delta.baseline_total} -> {delta.current_total} "
        f"({sign}{delta.rel_pct:.0f}%) breaches threshold "
        f"+{thresholds.default_err_rate_delta_pct:.0f}%"
    )


__all__ = [
    "KindDelta",
    "MetricDelta",
    "MetricSnapshot",
    "breaches_threshold",
    "capture_snapshot",
    "compute_delta",
    "watched_event_kinds",
]
