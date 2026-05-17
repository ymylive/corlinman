"""Error types for the hook bus.

Mirrors ``rust/crates/corlinman-hooks/src/error.rs``.
"""

from __future__ import annotations

__all__ = ["Closed", "HookCancelledError", "HookError", "Lagged", "RecvError"]


class HookError(Exception):
    """Base failure when publishing an event."""


class HookCancelledError(HookError):
    """The bus was cancelled before the event could be published.

    Mirrors the Rust ``HookError::Cancelled`` variant. The Rust enum has
    a single variant today; we model it as a subclass so callers can
    use ``except HookCancelledError`` directly while still being able
    to catch the umbrella :class:`HookError`.
    """

    def __init__(self) -> None:
        super().__init__("hook bus cancelled")


class RecvError(Exception):
    """Base failure when a subscriber pulls from the bus.

    Mirrors the Rust ``RecvError`` enum. Concrete subclasses are
    :class:`Closed` and :class:`Lagged`.
    """


class Closed(RecvError):  # noqa: N818 — mirrors Rust `RecvError::Closed`
    """The sender side of this priority tier has been dropped (the bus
    itself is gone)."""

    def __init__(self) -> None:
        super().__init__("hook bus closed")


class Lagged(RecvError):  # noqa: N818 — mirrors Rust `RecvError::Lagged`
    """The subscriber fell behind and ``count`` events were dropped."""

    def __init__(self, count: int) -> None:
        super().__init__(f"hook subscriber lagged by {count} events")
        self.count = count
