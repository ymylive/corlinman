"""Group ``evolution_signals`` rows into actionable clusters.

Phase 2 keeps this dead-simple: bucket by ``(event_kind, target)``. Phase 3
will swap in semantic similarity if needed; for now an identical pair of
strings is the only signal we trust.

A cluster is "actionable" when its size meets ``min_cluster_size``. Below
that threshold we silently drop — the design doc treats sub-threshold
groups as noise, not as a paged-out queue to revisit later.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from corlinman_evolution_engine.store import SignalRow


@dataclass(frozen=True)
class SignalCluster:
    """A bucket of signals sharing ``(event_kind, target)``."""

    event_kind: str
    target: str | None
    signals: list[SignalRow]

    @property
    def size(self) -> int:
        return len(self.signals)

    @property
    def signal_ids(self) -> list[int]:
        return [s.id for s in self.signals]

    @property
    def trace_ids(self) -> list[str]:
        # Preserve insertion order, dedup, drop None.
        seen: set[str] = set()
        out: list[str] = []
        for s in self.signals:
            if s.trace_id and s.trace_id not in seen:
                seen.add(s.trace_id)
                out.append(s.trace_id)
        return out


def cluster_signals(
    signals: list[SignalRow],
    *,
    min_cluster_size: int,
) -> list[SignalCluster]:
    """Group ``signals`` by ``(event_kind, target)``.

    Buckets smaller than ``min_cluster_size`` are dropped. Clusters are
    returned in descending size order so callers that respect a per-run
    proposal cap consume the strongest signals first.
    """
    if min_cluster_size < 1:
        raise ValueError(f"min_cluster_size must be >= 1, got {min_cluster_size}")

    groups: dict[tuple[str, str | None], list[SignalRow]] = defaultdict(list)
    for s in signals:
        groups[(s.event_kind, s.target)].append(s)

    clusters = [
        SignalCluster(event_kind=k[0], target=k[1], signals=members)
        for k, members in groups.items()
        if len(members) >= min_cluster_size
    ]
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters
