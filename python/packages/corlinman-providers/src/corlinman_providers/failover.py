"""Corlinman error hierarchy — mirrors the Rust ``FailoverReason`` enum.

Every provider adapter normalises vendor SDK errors to one of these types so
the Rust agent-client (via gRPC ``ErrorInfo``) can pick a retry policy from
``corlinman-core::backoff::DEFAULT_SCHEDULE`` and decide whether to fail over
to the next model in ``ModelRedirect.json``.

See plan §8 A1 and ``proto/corlinman/v1/common.proto::FailoverReason``.
"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "AuthPermanentError",
    "BillingError",
    "ContextOverflowError",
    "CorlinmanError",
    "FormatError",
    "ModelNotFoundError",
    "OverloadedError",
    "RateLimitError",
    "TimeoutError",
]


class CorlinmanError(Exception):
    """Base class for every provider-layer failure classified for failover.

    Attributes mirror the fields carried by ``common.proto::ErrorInfo``.
    """

    reason: str = "unknown"
    """Matches a variant of the Rust ``FailoverReason`` enum, lowercase."""

    status_code: int = 0
    """Upstream HTTP status code if available, else ``0``."""

    provider: str | None = None
    """Provider id (``"anthropic"``, ``"openai"`` …) when known."""

    model: str | None = None
    """Model id the call targeted, when known."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.model = model


class BillingError(CorlinmanError):
    """Payment / quota exhausted — never retry, fail over immediately."""

    reason = "billing"


class RateLimitError(CorlinmanError):
    """HTTP 429 or vendor-specific rate limit — retry after ``retry_after_ms``."""

    reason = "rate_limit"
    retry_after_ms: int | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        provider: str | None = None,
        model: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, provider=provider, model=model)
        self.retry_after_ms = retry_after_ms


class AuthError(CorlinmanError):
    """Transient auth failure (clock skew, token refresh race) — may retry once."""

    reason = "auth"


class AuthPermanentError(CorlinmanError):
    """Permanent auth failure (revoked key, wrong tenant) — do not retry."""

    reason = "auth_permanent"


class TimeoutError(CorlinmanError):  # noqa: A001 — shadowing builtins.TimeoutError is intentional
    """Upstream timeout — retry per ``BACKOFF_SCHEDULE``."""

    reason = "timeout"

    def __init__(
        self,
        message: str = "upstream timeout",
        *,
        status_code: int = 0,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, provider=provider, model=model)


class ModelNotFoundError(CorlinmanError):
    """Upstream says the model id is unknown — fail over without retry."""

    reason = "model_not_found"


class FormatError(CorlinmanError):
    """Response body violates the expected schema (bad JSON, missing fields)."""

    reason = "format"


class ContextOverflowError(CorlinmanError):
    """Prompt + expected completion exceed model context window."""

    reason = "context_overflow"


class OverloadedError(CorlinmanError):
    """Provider reports overload (HTTP 503 / vendor-specific) — retry later."""

    reason = "overloaded"
