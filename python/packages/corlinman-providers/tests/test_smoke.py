"""Smoke tests — error hierarchy wires up correctly and Protocol imports."""

from __future__ import annotations

from corlinman_providers import (
    AuthError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    CorlinmanProvider,
    OverloadedError,
    RateLimitError,
)


def test_all_errors_descend_from_corlinman_error() -> None:
    for exc_type in (BillingError, RateLimitError, AuthError, OverloadedError, ContextOverflowError):
        assert issubclass(exc_type, CorlinmanError)


def test_reason_tag_present() -> None:
    err = BillingError("no credit", status_code=402, provider="anthropic", model="claude-opus-4")
    assert err.reason == "billing"
    assert err.provider == "anthropic"
    assert err.model == "claude-opus-4"
    assert err.status_code == 402


def test_rate_limit_carries_retry_after() -> None:
    err = RateLimitError("slow down", retry_after_ms=1500, provider="openai")
    assert err.retry_after_ms == 1500
    assert err.reason == "rate_limit"


def test_provider_protocol_is_runtime_checkable() -> None:
    # A plain object doesn't implement the Protocol surface.
    assert not isinstance(object(), CorlinmanProvider)
