"""Retry orchestration for the agent gRPC client.

Ports ``corlinman-agent-client::retry`` and ``::classify``:

* :func:`classify_grpc_error` maps a ``grpc.StatusCode`` (+ trailing
  details string) onto a :class:`FailoverReason`. The mapping mirrors
  ``corlinman_core::CorlinmanError::grpc_code`` so a round-trip Rust
  enum → status → classify is stable.
* :func:`next_retry_delay` consults the shared backoff schedule
  ``[5s, 10s, 30s, 60s]`` — same as ``DEFAULT_SCHEDULE`` in Rust.
* :func:`status_to_error` builds an :class:`UpstreamError` from a
  ``grpc.aio.AioRpcError``.
* :func:`with_retry` runs an async operation up to ``len(schedule)+1``
  times, sleeping per the schedule, classifying each failure.

Non-retryable reasons (``AUTH_PERMANENT``, ``MODEL_NOT_FOUND``,
``CONTEXT_OVERFLOW``) short-circuit to ``None`` on attempt 0 so the
caller gives up immediately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import grpc
from grpc.aio import AioRpcError

from corlinman_grpc.agent_client.types import FailoverReason, UpstreamError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backoff schedule — mirrors corlinman_core::backoff::DEFAULT_SCHEDULE.
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0)
"""Wait (seconds) between retries. Length = max number of retries."""


# ---------------------------------------------------------------------------
# Status → FailoverReason classifier.
# ---------------------------------------------------------------------------


def classify_grpc_error(
    code: grpc.StatusCode | None, details: str | None = None
) -> FailoverReason:
    """Classify a gRPC status code into a :class:`FailoverReason`.

    ``code`` may be ``None`` when the failure isn't a gRPC ``StatusCode``
    (e.g. the channel never connected); we conservatively map that to
    :attr:`FailoverReason.UNKNOWN` so callers retry once.

    ``details`` is the trailing message — used only to disambiguate
    ``UNAUTHENTICATED`` between transient (``AUTH``) and permanent
    (``AUTH_PERMANENT``) revocations, mirroring the Rust heuristic.
    """
    if code is None:
        return FailoverReason.UNKNOWN
    if code == grpc.StatusCode.OK:
        return FailoverReason.UNSPECIFIED
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return FailoverReason.RATE_LIMIT
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return FailoverReason.TIMEOUT
    if code == grpc.StatusCode.UNAVAILABLE:
        return FailoverReason.OVERLOADED
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return _classify_auth(details or "")
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return FailoverReason.AUTH_PERMANENT
    if code == grpc.StatusCode.NOT_FOUND:
        return FailoverReason.MODEL_NOT_FOUND
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return FailoverReason.FORMAT
    if code == grpc.StatusCode.CANCELLED:
        return FailoverReason.UNSPECIFIED
    return FailoverReason.UNKNOWN


def _classify_auth(message: str) -> FailoverReason:
    """Flip ``AUTH`` to ``AUTH_PERMANENT`` on hard-revocation hints.

    Keyword list intentionally small to avoid false positives — keep it
    aligned with the Rust ``classify_auth`` heuristic.
    """
    lower = message.lower()
    if "revoked" in lower or "invalid_api_key" in lower or "permanent" in lower:
        return FailoverReason.AUTH_PERMANENT
    return FailoverReason.AUTH


# ---------------------------------------------------------------------------
# Error / delay helpers.
# ---------------------------------------------------------------------------


def status_to_error(err: AioRpcError) -> UpstreamError:
    """Convert an ``AioRpcError`` into an :class:`UpstreamError`.

    Kept here so callers don't need to know about :func:`classify_grpc_error`.
    """
    code = err.code()
    details = err.details() or ""
    reason = classify_grpc_error(code, details)
    return UpstreamError(reason=reason, message=details)


def next_retry_delay(
    attempt: int, err: AioRpcError
) -> tuple[float, FailoverReason] | None:
    """Inspect a failed gRPC call and decide the next wait.

    Returns ``(delay_seconds, reason)`` when the caller should retry, or
    ``None`` when the failure is terminal (non-retryable reason or
    schedule exhausted). ``attempt`` is zero-based.
    """
    reason = classify_grpc_error(err.code(), err.details() or "")
    if not reason.retryable():
        return None
    if attempt < 0 or attempt >= len(DEFAULT_SCHEDULE):
        return None
    return DEFAULT_SCHEDULE[attempt], reason


async def with_retry[T](
    op: Callable[[], Awaitable[T]],
    *,
    schedule: tuple[float, ...] = DEFAULT_SCHEDULE,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``op`` with up to ``len(schedule) + 1`` attempts.

    ``op`` is a no-arg coroutine factory so the caller can rebuild
    streams / channels per attempt (mirrors ``FnMut() -> Future`` on the
    Rust side). On every ``AioRpcError`` the schedule is consulted; on a
    terminal reason or schedule exhaustion the failure is wrapped in
    :class:`UpstreamError` and raised.

    ``sleep`` is injectable so tests can pause time without real waits.
    """
    attempt = 0
    while True:
        try:
            return await op()
        except AioRpcError as err:
            decision = _decide(attempt, err, schedule)
            if decision is None:
                raise status_to_error(err) from err
            delay, reason = decision
            logger.warning(
                "agent-client retrying after failure",
                extra={
                    "attempt": attempt,
                    "reason": reason.as_str(),
                    "delay_ms": int(delay * 1000),
                },
            )
            await sleep(delay)
            attempt += 1


def _decide(
    attempt: int, err: AioRpcError, schedule: tuple[float, ...]
) -> tuple[float, FailoverReason] | None:
    """Schedule-aware variant of :func:`next_retry_delay`."""
    reason = classify_grpc_error(err.code(), err.details() or "")
    if not reason.retryable():
        return None
    if attempt < 0 or attempt >= len(schedule):
        return None
    return schedule[attempt], reason


__all__ = [
    "DEFAULT_SCHEDULE",
    "classify_grpc_error",
    "next_retry_delay",
    "status_to_error",
    "with_retry",
]
