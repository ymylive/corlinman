"""Shared types for the agent gRPC client.

Mirrors the Rust ``corlinman-core`` types that ``corlinman-agent-client``
depends on (``FailoverReason``, ``CorlinmanError::Upstream``). Kept local
to this submodule so importing ``agent_client`` never drags in the rest
of the Python plane.

The variant order and ``int`` values of :class:`FailoverReason` are
**load-bearing**: they match ``proto/common.proto`` and Rust's
``#[repr(i32)]`` so a round-trip Rust enum <-> wire <-> Python enum is
stable. Same goes for :meth:`FailoverReason.retryable` — non-retryable
variants are exactly the three Rust marks (``AuthPermanent``,
``ModelNotFound``, ``ContextOverflow``).
"""

from __future__ import annotations

from enum import IntEnum


class FailoverReason(IntEnum):
    """Classified failure reason for provider failover + retry decisions.

    Mirrors ``corlinman_core::FailoverReason`` and
    ``corlinman.v1.FailoverReason``.
    """

    UNSPECIFIED = 0
    """Default / no reason yet attached."""

    BILLING = 1
    """Quota exhausted, card declined, org billing disabled."""

    RATE_LIMIT = 2
    """429-equivalent; retryable after provider-advertised backoff."""

    AUTH = 3
    """401/403 that may be transient (key rotation). Retry once."""

    AUTH_PERMANENT = 4
    """Revoked or structurally invalid key. **Do not retry.**"""

    TIMEOUT = 5
    """Upstream did not respond within deadline."""

    MODEL_NOT_FOUND = 6
    """404 on model id / provider doesn't host this model."""

    FORMAT = 7
    """Malformed response / JSON parse failed / SSE framing broken."""

    CONTEXT_OVERFLOW = 8
    """Prompt exceeds provider context window."""

    OVERLOADED = 9
    """503 / "overloaded_error" (anthropic)."""

    UNKNOWN = 10
    """Catch-all; treat as retryable **once**."""

    def retryable(self) -> bool:
        """Whether a caller should retry (possibly after backoff).

        ``AUTH_PERMANENT``, ``MODEL_NOT_FOUND`` and ``CONTEXT_OVERFLOW``
        are terminal; everything else respects the backoff schedule.
        """
        return self not in _NON_RETRYABLE

    def as_str(self) -> str:
        """Human-readable label used in metrics + structured logs.

        Matches the Rust ``FailoverReason::as_str`` taxonomy exactly so
        the two halves of the system share log-line tokens.
        """
        return _LABELS[self]


_NON_RETRYABLE: frozenset[FailoverReason] = frozenset(
    {
        FailoverReason.AUTH_PERMANENT,
        FailoverReason.MODEL_NOT_FOUND,
        FailoverReason.CONTEXT_OVERFLOW,
    }
)

_LABELS: dict[FailoverReason, str] = {
    FailoverReason.UNSPECIFIED: "unspecified",
    FailoverReason.BILLING: "billing",
    FailoverReason.RATE_LIMIT: "rate_limit",
    FailoverReason.AUTH: "auth",
    FailoverReason.AUTH_PERMANENT: "auth_permanent",
    FailoverReason.TIMEOUT: "timeout",
    FailoverReason.MODEL_NOT_FOUND: "model_not_found",
    FailoverReason.FORMAT: "format",
    FailoverReason.CONTEXT_OVERFLOW: "context_overflow",
    FailoverReason.OVERLOADED: "overloaded",
    FailoverReason.UNKNOWN: "unknown",
}


class AgentClientError(Exception):
    """Base error for the agent gRPC client."""


class ConfigError(AgentClientError):
    """Endpoint URI is malformed or the channel could not be opened."""


class UpstreamError(AgentClientError):
    """The Python agent (or anything behind it) returned a classifiable
    gRPC failure.

    Maps 1:1 onto ``corlinman_core::CorlinmanError::Upstream { reason,
    message }``. Callers (failover, retry loops) read :attr:`reason` to
    decide whether to retry, fail over, or surface the error.
    """

    def __init__(self, reason: FailoverReason, message: str) -> None:
        super().__init__(f"upstream {reason.as_str()}: {message}")
        self.reason = reason
        self.message = message

    def retryable(self) -> bool:
        return self.reason.retryable()


__all__ = [
    "AgentClientError",
    "ConfigError",
    "FailoverReason",
    "UpstreamError",
]
