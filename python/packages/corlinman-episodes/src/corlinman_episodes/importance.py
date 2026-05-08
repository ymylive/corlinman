"""Importance score for a :class:`SourceBundle`.

The rubric is taken verbatim from
``docs/design/phase4-w4-d1-design.md`` §"Importance scoring":

| Component                  | Weight                                     |
|----------------------------|--------------------------------------------|
| Per-source-signal density  | +0.05 per signal up to 10 (cap +0.5)        |
| Evolution apply outcome    | +0.2 per applied + +0.1 per auto-rollback   |
| Severity = critical        | +0.3 (single hit)                           |
| Severity = error           | +0.15 (single hit)                          |
| Operator action present    | +0.1                                        |
| Identity unified           | +0.15                                       |
| Onboarding kind            | +0.1 baseline                               |

Score = ``clip(sum(weights), 0, 1)``. Computed once at distillation
time and frozen on the row — a 3-month-old episode shouldn't shift
rank when a future weight is re-tuned.

The function is intentionally pure: same bundle (+ same kind) → same
score. The runner asserts that contract via the
``importance_score_pure_function_of_inputs`` test in the matrix.
"""

from __future__ import annotations

from corlinman_episodes.sources import SourceBundle
from corlinman_episodes.store import EpisodeKind

# Weight constants. Lifted out so the test matrix can grep them (the
# design doc doubles as the canonical spec — drift between the two
# would be load-bearing). Importance bounds are inclusive.
W_SIGNAL_PER_ROW: float = 0.05
W_SIGNAL_CAP: float = 0.5
W_APPLY: float = 0.2
W_AUTO_ROLLBACK: float = 0.1
W_SEVERITY_CRITICAL: float = 0.3
W_SEVERITY_ERROR: float = 0.15
W_OPERATOR_ACTION: float = 0.1
W_IDENTITY_UNIFIED: float = 0.15
W_ONBOARDING_BASELINE: float = 0.1

# Hook kinds that the rubric counts as "operator action present".
# Distinct from the classifier's set: an ``evolution_applied`` hook
# fires regardless of whether an operator pushed it, but in either
# case marks an operator-relevant moment for ranking purposes.
_OPERATOR_HOOK_KINDS: frozenset[str] = frozenset(
    {"tool_approved", "evolution_applied"}
)


def _signal_density_component(signal_count: int) -> float:
    """+0.05 per signal up to 10, capped at 0.5."""
    capped = min(signal_count, 10)
    return min(capped * W_SIGNAL_PER_ROW, W_SIGNAL_CAP)


def _evolution_outcome_component(bundle: SourceBundle) -> float:
    """Sum of apply + rollback weights from ``bundle.history``.

    The doc describes "each applied" and "each auto-rollback" as
    additive — a proposal that applied then rolled back contributes
    both weights. ``rollback_reason='auto_rollback'`` is the
    canonical marker; the auto-rollback monitor stamps it.
    """
    score = 0.0
    for h in bundle.history:
        score += W_APPLY
        if h.rolled_back_at_ms is not None and (
            h.rollback_reason and "auto_rollback" in h.rollback_reason.lower()
        ):
            score += W_AUTO_ROLLBACK
    return score


def _severity_component(bundle: SourceBundle) -> float:
    """Single-hit weight for the *highest* severity in the bundle.

    "single hit" in the doc means we don't double-count multiple
    critical signals — one critical-severity signal in the window
    earns the +0.3, ten don't earn +3.0. Ranks "critical > error".
    """
    severities = {s.severity.lower() for s in bundle.signals}
    if "critical" in severities:
        return W_SEVERITY_CRITICAL
    if "error" in severities:
        return W_SEVERITY_ERROR
    return 0.0


def _operator_component(bundle: SourceBundle) -> float:
    if any(h.kind in _OPERATOR_HOOK_KINDS for h in bundle.hooks):
        return W_OPERATOR_ACTION
    return 0.0


def _identity_component(bundle: SourceBundle) -> float:
    if bundle.identity_merges:
        return W_IDENTITY_UNIFIED
    return 0.0


def _onboarding_component(kind: EpisodeKind) -> float:
    if kind == EpisodeKind.ONBOARDING:
        return W_ONBOARDING_BASELINE
    return 0.0


def score(bundle: SourceBundle, kind: EpisodeKind) -> float:
    """Pure function — same inputs always return the same float.

    The result is clamped to ``[0.0, 1.0]``. A floor of 0 is fine —
    episodes still get written, they just sort last in the importance
    indexes.
    """
    raw = (
        _signal_density_component(len(bundle.signals))
        + _evolution_outcome_component(bundle)
        + _severity_component(bundle)
        + _operator_component(bundle)
        + _identity_component(bundle)
        + _onboarding_component(kind)
    )
    return max(0.0, min(1.0, raw))


__all__ = [
    "W_APPLY",
    "W_AUTO_ROLLBACK",
    "W_IDENTITY_UNIFIED",
    "W_ONBOARDING_BASELINE",
    "W_OPERATOR_ACTION",
    "W_SEVERITY_CRITICAL",
    "W_SEVERITY_ERROR",
    "W_SIGNAL_CAP",
    "W_SIGNAL_PER_ROW",
    "score",
]
