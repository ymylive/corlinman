"""Retry classification + backoff orchestration."""

from __future__ import annotations

from typing import Any

import grpc
import pytest
from corlinman_grpc.agent_client import (
    DEFAULT_SCHEDULE,
    FailoverReason,
    UpstreamError,
    classify_grpc_error,
    next_retry_delay,
    status_to_error,
    with_retry,
)
from grpc.aio import AioRpcError


def _err(code: grpc.StatusCode, details: str = "") -> AioRpcError:
    """Build a synthetic ``AioRpcError`` without going through a real RPC."""
    return AioRpcError(
        code=code,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details=details,
    )


# ---------------------------------------------------------------------------
# classify_grpc_error — table-driven parity with the Rust tests.
# ---------------------------------------------------------------------------


def test_resource_exhausted_is_rate_limit() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, "quota")
        is FailoverReason.RATE_LIMIT
    )


def test_deadline_is_timeout() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, "slow")
        is FailoverReason.TIMEOUT
    )


def test_unavailable_is_overloaded() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.UNAVAILABLE, "backpressure")
        is FailoverReason.OVERLOADED
    )


def test_unauthenticated_default_is_auth() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.UNAUTHENTICATED, "token expired")
        is FailoverReason.AUTH
    )


def test_unauthenticated_revoked_is_permanent() -> None:
    assert (
        classify_grpc_error(
            grpc.StatusCode.UNAUTHENTICATED, "invalid_api_key: key revoked"
        )
        is FailoverReason.AUTH_PERMANENT
    )


def test_permission_denied_is_auth_permanent() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.PERMISSION_DENIED, "")
        is FailoverReason.AUTH_PERMANENT
    )


def test_not_found_is_model_not_found() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.NOT_FOUND, "no such model")
        is FailoverReason.MODEL_NOT_FOUND
    )


def test_invalid_argument_is_format() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.INVALID_ARGUMENT, "bad json")
        is FailoverReason.FORMAT
    )


def test_cancelled_is_unspecified() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.CANCELLED, "")
        is FailoverReason.UNSPECIFIED
    )


def test_internal_is_unknown() -> None:
    assert (
        classify_grpc_error(grpc.StatusCode.INTERNAL, "boom")
        is FailoverReason.UNKNOWN
    )


def test_none_code_is_unknown() -> None:
    # Non-RPC failures (e.g. channel never connected) classify safely.
    assert classify_grpc_error(None, None) is FailoverReason.UNKNOWN


# ---------------------------------------------------------------------------
# next_retry_delay — schedule consultation.
# ---------------------------------------------------------------------------


def test_retry_delay_for_rate_limit() -> None:
    err = _err(grpc.StatusCode.RESOURCE_EXHAUSTED, "slow")
    decision = next_retry_delay(0, err)
    assert decision is not None
    delay, reason = decision
    assert delay == 5.0
    assert reason is FailoverReason.RATE_LIMIT


def test_retry_delay_returns_none_for_not_found() -> None:
    err = _err(grpc.StatusCode.NOT_FOUND, "gone")
    assert next_retry_delay(0, err) is None


def test_retry_delay_exhausted() -> None:
    err = _err(grpc.StatusCode.UNAVAILABLE, "down")
    # Schedule has 4 entries — attempt 4 is past the end.
    assert next_retry_delay(len(DEFAULT_SCHEDULE), err) is None


def test_schedule_matches_rust() -> None:
    assert DEFAULT_SCHEDULE == (5.0, 10.0, 30.0, 60.0)


# ---------------------------------------------------------------------------
# status_to_error — preserves the reason on the exception.
# ---------------------------------------------------------------------------


def test_status_to_error_preserves_reason() -> None:
    err = _err(grpc.StatusCode.RESOURCE_EXHAUSTED, "x")
    upstream = status_to_error(err)
    assert isinstance(upstream, UpstreamError)
    assert upstream.reason is FailoverReason.RATE_LIMIT
    assert upstream.message == "x"


# ---------------------------------------------------------------------------
# with_retry — drives the schedule with a fake sleep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_returns_value_on_success() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    out = await with_retry(op)
    assert out == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_retries_until_success() -> None:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _err(grpc.StatusCode.UNAVAILABLE, "transient")
        return "done"

    out = await with_retry(op, sleep=fake_sleep)
    assert out == "done"
    assert attempts == 3
    # Two retries → two sleeps with the first two schedule entries.
    assert sleeps == [5.0, 10.0]


@pytest.mark.asyncio
async def test_with_retry_gives_up_on_terminal() -> None:
    async def op() -> Any:
        raise _err(grpc.StatusCode.NOT_FOUND, "model gone")

    with pytest.raises(UpstreamError) as ei:
        await with_retry(op)
    assert ei.value.reason is FailoverReason.MODEL_NOT_FOUND


@pytest.mark.asyncio
async def test_with_retry_exhausts_schedule_then_raises() -> None:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    async def op() -> Any:
        raise _err(grpc.StatusCode.UNAVAILABLE, "down")

    with pytest.raises(UpstreamError) as ei:
        await with_retry(op, sleep=fake_sleep)
    assert ei.value.reason is FailoverReason.OVERLOADED
    # 4 retries → 4 sleeps, then the 5th attempt fails terminally.
    assert sleeps == list(DEFAULT_SCHEDULE)
