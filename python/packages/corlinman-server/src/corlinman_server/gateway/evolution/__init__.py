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
