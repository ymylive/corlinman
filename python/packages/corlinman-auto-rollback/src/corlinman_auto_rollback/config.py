"""Config shapes for the AutoRollback monitor.

Ported 1:1 from the Rust ``EvolutionAutoRollbackConfig`` /
``AutoRollbackThresholds`` in ``rust/crates/corlinman-core/src/config.rs``.
Kept here (and not in ``corlinman-evolution-store``) so the monitor stays
self-contained — there is no Python sibling of ``corlinman-core``'s
config crate yet, so the lightweight dataclass mirror lives here.

Defaults are the conservative ones the Rust crate ships with:

* master switch ``enabled = False`` — the CLI hard-errors when the
  operator hasn't opted in.
* ``grace_window_hours = 72`` — long enough to catch slow-burn
  regressions, short enough not to revert ancient applies out from
  under newer state.
* ``signal_window_secs = 1800`` (30 min), ``min_baseline_signals = 5``
  (quiet-target guard), ``default_err_rate_delta_pct = 50.0``,
  ``default_p95_latency_delta_pct = 25.0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AutoRollbackThresholds:
    """Threshold knobs the monitor uses to decide whether a metrics
    delta warrants a rollback. Mirror of the Rust ``AutoRollbackThresholds``.

    * ``default_err_rate_delta_pct`` — maximum percent increase in
      error-severity signal count over baseline before triggering
      rollback. ``50.0`` means "+50%".
    * ``default_p95_latency_delta_pct`` — reserved for future kinds
      that emit latency signals; memory_op today doesn't use it.
    * ``signal_window_secs`` — sliding-window length used both
      pre-apply (baseline) and post-apply (current) when counting
      signals. Keeping them symmetric prevents a false positive from
      sample-window mismatch.
    * ``min_baseline_signals`` — minimum baseline count required
      before a percent delta is trusted; guards against
      "0 -> 1 = +infinity%" false positives on quiet targets.
    """

    default_err_rate_delta_pct: float = 50.0
    default_p95_latency_delta_pct: float = 25.0
    signal_window_secs: int = 1_800
    min_baseline_signals: int = 5


@dataclass
class EvolutionAutoRollbackConfig:
    """Top-level AutoRollback config block, mirrored from Rust."""

    enabled: bool = False
    grace_window_hours: int = 72
    thresholds: AutoRollbackThresholds = field(default_factory=AutoRollbackThresholds)


__all__ = [
    "AutoRollbackThresholds",
    "EvolutionAutoRollbackConfig",
]
