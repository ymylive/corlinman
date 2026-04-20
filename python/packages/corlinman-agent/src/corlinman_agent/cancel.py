"""Cancellation helpers — ``asyncio.timeout`` + ``CancelledError`` wrappers.

Mirrors ``corlinman-core::cancel::{combine, with_timeout}`` on the Rust side
(plan §8 A2). Used by the reasoning loop, provider adapters, and the
embedding router so a single cancel signal collapses every outstanding I/O.

TODO(M2): implement ``combine`` (merge multiple ``asyncio.Event`` / cancel
scopes) and ``with_timeout`` (raise :class:`corlinman_providers.TimeoutError`
on expiry, translate ``asyncio.CancelledError`` to a semantic result).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import structlog
from corlinman_providers import TimeoutError as CorlinmanTimeoutError

logger = structlog.get_logger(__name__)


async def with_timeout[T](awaitable: Awaitable[T], *, seconds: float) -> T:
    """Run ``awaitable`` under ``asyncio.timeout`` and translate timeouts.

    On expiry raises :class:`corlinman_providers.TimeoutError` so the
    agent-client can classify it as ``FailoverReason::Timeout``.
    """
    try:
        async with asyncio.timeout(seconds):
            return await awaitable
    except TimeoutError as exc:  # builtins.TimeoutError — the one asyncio.timeout raises
        raise CorlinmanTimeoutError(
            f"operation exceeded {seconds:.1f}s",
        ) from exc
