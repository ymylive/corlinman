"""Group ``evolution_signals`` rows into actionable clusters.

Phase 2 keeps this dead-simple: bucket by ``(event_kind, target)``. Phase 3
will swap in semantic similarity if needed; for now an identical pair of
strings is the only signal we trust.

A cluster is "actionable" when its size meets ``min_cluster_size``. Below
that threshold we silently drop — the design doc treats sub-threshold
groups as noise, not as a paged-out queue to revisit later.

Phase 4 W1 4-1A added ``tenant_id`` to ``evolution_signals``; the
clustering key now folds it in so a deployment serving multiple tenants
never accidentally merges two tenants' signals into one cluster (and
therefore one proposal). Pre-4-1A signal rows materialise with
``tenant_id="default"`` so existing single-tenant tests cluster
identically to before.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from corlinman_evolution_engine.store import DEFAULT_TENANT_ID, SignalRow


@dataclass(frozen=True)
class SignalCluster:
    """A bucket of signals sharing ``(event_kind, target, tenant_id)``."""

    event_kind: str
    target: str | None
    signals: list[SignalRow]
    tenant_id: str = DEFAULT_TENANT_ID

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
    """Group ``signals`` by ``(event_kind, target, tenant_id)``.

    Buckets smaller than ``min_cluster_size`` are dropped. Clusters are
    returned in descending size order so callers that respect a per-run
    proposal cap consume the strongest signals first.

    The ``tenant_id`` axis defends multi-tenant deployments: two tenants
    with the same ``(event_kind, target)`` produce two independent
    clusters, never one merged super-cluster. Single-tenant deployments
    behave identically to Phase 2/3 because every row defaults to
    ``"default"``.
    """
    if min_cluster_size < 1:
        raise ValueError(f"min_cluster_size must be >= 1, got {min_cluster_size}")

    groups: dict[tuple[str, str | None, str], list[SignalRow]] = defaultdict(list)
    for s in signals:
        tenant = s.tenant_id or DEFAULT_TENANT_ID
        groups[(s.event_kind, s.target, tenant)].append(s)

    clusters = [
        SignalCluster(
            event_kind=k[0],
            target=k[1],
            signals=members,
            tenant_id=k[2],
        )
        for k, members in groups.items()
        if len(members) >= min_cluster_size
    ]
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters
