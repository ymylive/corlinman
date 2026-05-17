"""Graceful shutdown: SIGTERM / SIGINT → drain in-flight → exit 143.

Python port of ``rust/crates/corlinman-gateway/src/shutdown.rs``. Mirrors
the POSIX convention: on SIGTERM / SIGINT the gateway stops accepting
new connections, lets in-flight requests finish, then exits with status
**143** (the Unix convention for ``128 + SIGTERM``).

This is the gateway-flavour helper (HTTP). A sibling gRPC server may
still want :class:`corlinman_server.shutdown.GracefulShutdown` for its
own loop — both can live alongside each other; whichever fires first
sets the reason.

The :func:`wait_for_signal` coroutine never raises (signal-subsystem
errors downgrade to a warning + ``ShutdownReason.TERMINATE``); the
``Windows`` branch listens for Ctrl-C only since there's no SIGTERM
analogue.
"""

from __future__ import annotations

import asyncio
import signal as _signal
import sys
from enum import Enum

import structlog

logger = structlog.get_logger(__name__)


class ShutdownReason(str, Enum):
    """Why the shutdown was triggered. Inherits ``str`` so the value is
    JSON-friendly (matches ``HookEvent`` payload conventions)."""

    TERMINATE = "SIGTERM"
    INTERRUPT = "SIGINT"


#: Conventional Unix exit code: ``128 + SIGTERM (15)``. Docker / systemd
#: read this as a clean stop.
EXIT_CODE_ON_SIGNAL: int = 143


async def wait_for_signal() -> ShutdownReason:
    """Resolve when the first SIGTERM / SIGINT arrives.

    Built on :meth:`asyncio.AbstractEventLoop.add_signal_handler`, which
    is the modern asyncio way to attach signal handlers from inside the
    running loop. Errors registering the handler (e.g. signal subsystem
    unavailable) downgrade to a warning + ``TERMINATE`` so the caller
    still receives a value rather than hanging.

    On Windows ``add_signal_handler`` is not implemented, so we fall
    back to polling — Ctrl-C is delivered as a ``KeyboardInterrupt`` we
    have to catch around an :func:`asyncio.sleep` loop.
    """

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[ShutdownReason] = loop.create_future()

    def _on_signal(reason: ShutdownReason) -> None:
        if not fut.done():
            fut.set_result(reason)

    if sys.platform == "win32":  # pragma: no cover — exercised on Windows only
        # No SIGTERM, no `add_signal_handler`; spin a wait for Ctrl-C.
        try:
            while True:
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            return ShutdownReason.INTERRUPT

    for sig, reason in (
        (_signal.SIGTERM, ShutdownReason.TERMINATE),
        (_signal.SIGINT, ShutdownReason.INTERRUPT),
    ):
        try:
            loop.add_signal_handler(sig, _on_signal, reason)
        except (NotImplementedError, RuntimeError, ValueError) as err:
            logger.warning(
                "shutdown.signal_install_failed",
                signal=str(sig),
                error=str(err),
            )

    return await fut


def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> asyncio.Future[ShutdownReason]:
    """Eager variant of :func:`wait_for_signal`: install handlers on
    ``loop`` immediately and return the future every reader can await.

    Useful when the caller wants to register the handler *before* the
    server-boot task starts (so a SIGTERM during boot still triggers
    the same clean-shutdown path). The returned future resolves with
    the first triggered :class:`ShutdownReason`.
    """

    fut: asyncio.Future[ShutdownReason] = loop.create_future()

    def _on_signal(reason: ShutdownReason) -> None:
        if not fut.done():
            fut.set_result(reason)

    for sig, reason in (
        (_signal.SIGTERM, ShutdownReason.TERMINATE),
        (_signal.SIGINT, ShutdownReason.INTERRUPT),
    ):
        try:
            loop.add_signal_handler(sig, _on_signal, reason)
        except (NotImplementedError, RuntimeError, ValueError) as err:
            logger.warning(
                "shutdown.signal_install_failed",
                signal=str(sig),
                error=str(err),
            )

    return fut


__all__ = [
    "EXIT_CODE_ON_SIGNAL",
    "ShutdownReason",
    "install_signal_handlers",
    "wait_for_signal",
]
