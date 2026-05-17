"""Errors emitted by the identity store, resolver, and verification protocol.

Variants intentionally mirror the Rust ``IdentityError`` enum 1:1 so the
admin REST surface (which serialises these into HTTP envelopes) can
share a single error vocabulary across the Rust gateway and the Python
plane.
"""

from __future__ import annotations

from pathlib import Path


class IdentityError(Exception):
    """Base class for every identity-layer error.

    Subclasses split into three groups (mirrors the Rust enum):

    1. **Storage** — schema bootstrap or SQL execution failed.
    2. **Resolver** — input was structurally invalid (empty channel
       name, etc.) or referenced a missing entity.
    3. **Verification** — phrase exchange protocol violations
       (expired / consumed / unknown).
    """


class StorageError(IdentityError):
    """Schema bootstrap or SQL execution failed.

    Carries the ``op`` label that the Rust enum uses (``"lookup"``,
    ``"redeem_commit"``, etc.) plus the original exception. Tests
    structurally match the variant; production code surfaces the
    ``op`` and message to operators via ``tracing``.
    """

    def __init__(self, op: str, source: BaseException) -> None:
        self.op = op
        self.source = source
        super().__init__(f"identity store {op}: {source}")


class OpenError(IdentityError):
    """Path-level open failure.

    Distinct from :class:`StorageError` because the remediation differs
    (filesystem permissions vs DB corruption).
    """

    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"identity store open failed at {path}: {source}")


class InvalidInputError(IdentityError):
    """Caller passed an empty ``channel``, ``channel_user_id``, or other
    structurally invalid input."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"invalid input: {message}")


class UserNotFoundError(IdentityError):
    """Lookup target doesn't exist (admin merge, alias_for, etc.)."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(f"user_id not found: {user_id!r}")


class PhraseUnknownError(IdentityError):
    """Verification phrase doesn't match any active row."""

    def __init__(self) -> None:
        super().__init__("verification phrase unknown")


class PhraseExpiredError(IdentityError):
    """Phrase exists but is past its ``expires_at``."""

    def __init__(self) -> None:
        super().__init__("verification phrase expired")


class PhraseAlreadyConsumedError(IdentityError):
    """Phrase was already redeemed."""

    def __init__(self) -> None:
        super().__init__("verification phrase already consumed")


__all__ = [
    "IdentityError",
    "InvalidInputError",
    "OpenError",
    "PhraseAlreadyConsumedError",
    "PhraseExpiredError",
    "PhraseUnknownError",
    "StorageError",
    "UserNotFoundError",
]
