"""Memory chunk decay arithmetic — Python port of `corlinman_vector::decay`.

Chunks accumulate a ``decay_score`` column. Each recall event applies a
``recall_boost``; reads at query time apply
``score * 2^(-age_hours / half_life_hours)`` so a chunk's effective relevance
fades unless it's actively recalled. Promotion to the ``consolidated``
namespace makes a chunk immune — ``decay_score`` stops changing and the
read-time decay multiplier collapses to 1.0.

Pure functions only — the SqliteStore in :mod:`corlinman_embedding.vector.bm25_store`
is the callsite. Keeping the math here means we can unit-test the half-life
curve without an SQLite roundtrip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["DecayConfig", "CONSOLIDATED_NAMESPACE", "apply_decay", "boosted_score"]


#: Namespace that's exempt from decay. Promoted chunks land here via
#: ``SqliteStore.promote_to_consolidated``.
CONSOLIDATED_NAMESPACE: str = "consolidated"


@dataclass(frozen=True)
class DecayConfig:
    """Tunables that drive the decay arithmetic. Mirrors Rust's ``DecayConfig``."""

    #: Master switch. When ``False`` :func:`apply_decay` returns ``score`` unchanged.
    enabled: bool = True
    #: Age (in hours) at which the decayed score is half the current ``decay_score``.
    half_life_hours: float = 168.0
    #: Floor below which the decayed score is clamped.
    floor_score: float = 0.05
    #: Bump applied to ``decay_score`` on every recall (capped at 1.0).
    recall_boost: float = 0.3


def apply_decay(score: float, age_hours: float, namespace: str, cfg: DecayConfig) -> float:
    """Apply exponential half-life decay.

    Semantics (mirrors Rust):

    - ``cfg.enabled == False`` → return ``score`` unchanged.
    - ``namespace == "consolidated"`` → return ``score`` unchanged (immune).
    - Otherwise: ``score * 2^(-age/half_life)``, clamped at ``cfg.floor_score``.

    Negative or non-finite ages are clamped to 0.
    """

    if not cfg.enabled:
        return score
    if namespace == CONSOLIDATED_NAMESPACE:
        return score
    age = age_hours if (math.isfinite(age_hours) and age_hours > 0.0) else 0.0
    if not (math.isfinite(cfg.half_life_hours) and cfg.half_life_hours > 0.0):
        # Misconfigured zero half-life would NaN-infect the pipeline.
        return score
    factor = math.exp(-age / cfg.half_life_hours * math.log(2.0))
    decayed = score * factor
    return max(decayed, cfg.floor_score)


def boosted_score(current: float, recall_boost: float) -> float:
    """Apply ``recall_boost`` to a current ``decay_score``, capped at 1.0."""

    return min(current + recall_boost, 1.0)
