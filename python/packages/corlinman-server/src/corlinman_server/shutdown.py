"""Graceful shutdown coordinator — SIGTERM → exit 143 (plan §8 A5).

Provides :class:`GracefulShutdown`, an ``asyncio``-friendly event that
:func:`corlinman_server.main._serve` awaits. Signal handlers call
``request(signal_name)``; ``wait()`` resolves with the signal name so the
entrypoint can pick the right exit code.

TODO(M1): add a "drain" phase that refuses new RPCs but lets in-flight
streams finish up to ``drain_deadline_seconds``.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


class GracefulShutdown:
    """One-shot asyncio shutdown signal carrying the triggering signal name."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    def request(self, reason: str) -> None:
        """Trigger shutdown; idempotent, records the first caller's ``reason``."""
        if self._reason is None:
            self._reason = reason
            logger.info("shutdown.requested", reason=reason)
            self._event.set()

    async def wait(self) -> str:
        """Block until :meth:`request` is called; return the reason tag."""
        await self._event.wait()
        assert self._reason is not None  # set before event.set()
        return self._reason
