"""Per-kind signal detectors that feed the evolution-store.

This package hosts heuristics that watch the live agent surface (chat
messages, tool outcomes, …) and emit ``EvolutionSignal`` rows into the
shared ``evolution_signals`` SQLite table. Each detector is a small,
fast, pure-Python function (no LLM) so it can run inline on the hot
chat path without measurable latency cost.

The first inhabitant is :mod:`.user_correction` (Wave 4.5) — a
heuristic spotter for corrective phrases in user messages that fires
:data:`corlinman_evolution_store.EVENT_USER_CORRECTION`. Future
detectors (e.g. tool-failure cluster spotter, sentiment shift) will
land alongside it.
"""

from corlinman_server.gateway.evolution.signals.user_correction import (
    CorrectionMatch,
    detect_correction,
    register_user_correction_listener,
)

__all__ = [
    "CorrectionMatch",
    "detect_correction",
    "register_user_correction_listener",
]
