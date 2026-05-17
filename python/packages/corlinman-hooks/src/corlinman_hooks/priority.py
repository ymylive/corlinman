"""Priority tiers + cooperative cancel.

The bus fans out one tier at a time, highest-priority first, so a
``Critical`` hook always observes an event before a ``Normal`` or
``Low`` one does. :class:`CancelToken` is a thin wrapper around a
boolean flag: emitters check it before publishing each tier, and
external code flips it to signal "stop emitting new events".

Mirrors ``rust/crates/corlinman-hooks/src/priority.rs``. Python's GIL
makes the atomic dance unnecessary — a plain instance attribute is
sufficient for the cancel flag's "stop emitting" semantics.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["CancelToken", "HookPriority"]


class HookPriority(Enum):
    """Subscribers pick a tier when they subscribe.

    :meth:`HookBus.emit` publishes in the order
    ``CRITICAL -> NORMAL -> LOW`` so a Critical subscriber is guaranteed
    to observe the event before any Normal/Low subscriber on a
    single-threaded asyncio runtime.
    """

    CRITICAL = "critical"
    NORMAL = "normal"
    LOW = "low"

    @classmethod
    def ordered(cls) -> tuple[HookPriority, HookPriority, HookPriority]:
        """Iteration order used by :meth:`HookBus.emit`.

        Critical first, Low last. Mirrors the Rust ``ordered()``
        associated function.
        """
        return (cls.CRITICAL, cls.NORMAL, cls.LOW)


class CancelToken:
    """Cooperative cancellation flag shared between emitter and subscribers.

    Cheap to copy by reference — Python objects are reference-typed by
    default, so passing a ``CancelToken`` around shares state without
    any explicit ``Arc`` analogue.

    Emitters check :meth:`is_cancelled` before publishing; callers flip
    it via :meth:`cancel` to drain the bus.
    """

    __slots__ = ("_flag",)

    def __init__(self) -> None:
        self._flag = False

    def cancel(self) -> None:
        self._flag = True

    def is_cancelled(self) -> bool:
        return self._flag

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"CancelToken(cancelled={self._flag})"
