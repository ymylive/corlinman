"""``corlinman_server.gateway.evolution`` — EvolutionApplier + EvolutionObserver.

Mirrors :rust:`corlinman_gateway::evolution_applier` and
:rust:`corlinman_gateway::evolution_observer`. Splits the Rust pair into
two Python modules:

* :mod:`corlinman_server.gateway.evolution.applier` —
  :class:`EvolutionApplier`, the concrete
  :class:`corlinman_auto_rollback.Applier` for ``memory_op`` proposals.
  Ties the evolution-store and the auto-rollback monitor together.
* :mod:`corlinman_server.gateway.evolution.observer` —
  :class:`EvolutionObserver`, the passive watcher that subscribes to
  the shared :class:`corlinman_hooks.HookBus`, adapts a curated subset
  of :class:`corlinman_hooks.HookEvent` variants into
  :class:`corlinman_evolution_store.EvolutionSignal` rows, and persists
  them via :class:`corlinman_evolution_store.SignalsRepo`.

The Rust gateway runs both side-by-side; the Python port keeps that
shape — the gateway boot path constructs the observer first (it has no
runtime dependency on the applier) and the applier second (it consumes
the same ``evolution.sqlite`` the observer writes to).
"""

from __future__ import annotations

from corlinman_server.gateway.evolution.applier import (
    ApplyError,
    EvolutionApplier,
    UnsupportedKindError,
)
from corlinman_server.gateway.evolution.observer import (
    EvolutionObserver,
    EvolutionObserverConfig,
    adapt,
)

__all__ = [
    "ApplyError",
    "EvolutionApplier",
    "EvolutionObserver",
    "EvolutionObserverConfig",
    "UnsupportedKindError",
    "adapt",
]

# ---------------------------------------------------------------------------
# W4.3 curator surface — additive re-export so sibling agent (W4.4
# background_review) can append below without rewriting this block.
# ---------------------------------------------------------------------------
from corlinman_server.gateway.evolution.curator import (  # noqa: E402
    CuratorReport,
    CuratorTransition,
    apply_lifecycle_transitions,
    maybe_run_curator,
)

__all__ += [
    "CuratorReport",
    "CuratorTransition",
    "apply_lifecycle_transitions",
    "maybe_run_curator",
]

# ---------------------------------------------------------------------------
# W4.4 background-review surface — additive re-export. The LLM-driven
# review fork is an opt-in companion to the deterministic curator above;
# both share the evolution-store and skills-registry but never call each
# other.
# ---------------------------------------------------------------------------
from corlinman_server.gateway.evolution.background_review import (  # noqa: E402
    BackgroundReviewReport,
    ReviewKind,
    ReviewWriteRecord,
    spawn_background_review,
)

__all__ += [
    "BackgroundReviewReport",
    "ReviewKind",
    "ReviewWriteRecord",
    "spawn_background_review",
]

# ---------------------------------------------------------------------------
# W4.5 user-correction surface — additive re-export. The detector +
# applier route corrective phrases in chat to a background-review fork
# scoped to ``kind="user-correction"``. Kept in their own modules so
# config can disable the routing without touching :mod:`.applier`.
# ---------------------------------------------------------------------------
from corlinman_server.gateway.evolution.signals.user_correction import (  # noqa: E402
    CorrectionMatch,
    detect_correction,
    register_user_correction_listener,
)
from corlinman_server.gateway.evolution.applier_user_correction import (  # noqa: E402
    UserCorrectionApplier,
)

__all__ += [
    "CorrectionMatch",
    "UserCorrectionApplier",
    "detect_correction",
    "register_user_correction_listener",
]
