"""``FailoverReason`` parity with the Rust enum."""

from __future__ import annotations

from corlinman_grpc.agent_client import FailoverReason, UpstreamError


def test_failover_reason_int_values_are_load_bearing() -> None:
    # The discriminant order mirrors proto/common.proto + the Rust enum.
    assert FailoverReason.UNSPECIFIED == 0
    assert FailoverReason.BILLING == 1
    assert FailoverReason.RATE_LIMIT == 2
    assert FailoverReason.AUTH == 3
    assert FailoverReason.AUTH_PERMANENT == 4
    assert FailoverReason.TIMEOUT == 5
    assert FailoverReason.MODEL_NOT_FOUND == 6
    assert FailoverReason.FORMAT == 7
    assert FailoverReason.CONTEXT_OVERFLOW == 8
    assert FailoverReason.OVERLOADED == 9
    assert FailoverReason.UNKNOWN == 10


def test_retryable_matches_rust() -> None:
    non_retryable = {
        FailoverReason.AUTH_PERMANENT,
        FailoverReason.MODEL_NOT_FOUND,
        FailoverReason.CONTEXT_OVERFLOW,
    }
    for r in FailoverReason:
        assert r.retryable() is (r not in non_retryable)


def test_as_str_labels() -> None:
    assert FailoverReason.RATE_LIMIT.as_str() == "rate_limit"
    assert FailoverReason.AUTH_PERMANENT.as_str() == "auth_permanent"
    assert FailoverReason.MODEL_NOT_FOUND.as_str() == "model_not_found"
    assert FailoverReason.CONTEXT_OVERFLOW.as_str() == "context_overflow"


def test_upstream_error_carries_reason() -> None:
    err = UpstreamError(FailoverReason.RATE_LIMIT, "slow down")
    assert err.reason is FailoverReason.RATE_LIMIT
    assert err.message == "slow down"
    assert "rate_limit" in str(err)
    assert err.retryable() is True

    perma = UpstreamError(FailoverReason.AUTH_PERMANENT, "revoked")
    assert perma.retryable() is False
