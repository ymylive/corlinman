"""corlinman AutoRollback (Python port).

Python sibling of the merged Rust crates ``corlinman-auto-rollback``
(library) and ``corlinman-auto-rollback-cli`` (binary).

Sits downstream of the EvolutionApplier. Once a proposal reaches
``applied`` status the monitor periodically checks: did the metrics
for the target the proposal touched degrade beyond a configurable
threshold relative to the baseline snapshot captured at apply time?
When yes, the monitor calls into an injected :class:`Applier` whose
implementation handles the actual revert path (parsing
``inverse_diff``, replaying the inverse mutation, stamping
``rolled_back_at`` / ``rollback_reason`` on the history row, flipping
``status`` to ``rolled_back``).

Module layout mirrors the Rust crate:

* :mod:`corlinman_auto_rollback.metrics` — signal-stream snapshot +
  delta computation.
* :mod:`corlinman_auto_rollback.revert` — :class:`Applier` protocol +
  typed :class:`RevertError` set.
* :mod:`corlinman_auto_rollback.monitor` — orchestration.
* :mod:`corlinman_auto_rollback.config` — config dataclasses (mirror
  of ``corlinman-core::config::EvolutionAutoRollbackConfig``).
* :mod:`corlinman_auto_rollback.cli` — argparse CLI mirroring the
  Rust binary.
"""

from __future__ import annotations

from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.metrics import (
    KindDelta,
    MetricDelta,
    MetricSnapshot,
    breaches_threshold,
    capture_snapshot,
    compute_delta,
    watched_event_kinds,
)
from corlinman_auto_rollback.monitor import (
    DEFAULT_MAX_PROPOSALS_PER_RUN,
    AutoRollbackMonitor,
    RunSummary,
    now_ms,
)
from corlinman_auto_rollback.revert import (
    Applier,
    HistoryMissingRevertError,
    InternalRevertError,
    NotAppliedRevertError,
    NotFoundRevertError,
    RevertError,
    UnsupportedKindRevertError,
)

__all__ = [
    "DEFAULT_MAX_PROPOSALS_PER_RUN",
    "Applier",
    "AutoRollbackMonitor",
    "AutoRollbackThresholds",
    "EvolutionAutoRollbackConfig",
    "HistoryMissingRevertError",
    "InternalRevertError",
    "KindDelta",
    "MetricDelta",
    "MetricSnapshot",
    "NotAppliedRevertError",
    "NotFoundRevertError",
    "RevertError",
    "RunSummary",
    "UnsupportedKindRevertError",
    "breaches_threshold",
    "capture_snapshot",
    "compute_delta",
    "now_ms",
    "watched_event_kinds",
]
