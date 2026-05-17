"""Public type vocabulary for the identity layer.

Mirrors ``rust/crates/corlinman-identity/src/types.rs`` 1:1 so the wire
shapes (``UserId``, ``ChannelAlias``, ``BindingKind``,
``VerificationPhrase``) round-trip cleanly between the Rust gateway and
the Python plane.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# ---------------------------------------------------------------------------
# ULID — minimal in-process generator so we don't pull in another dep.
# ---------------------------------------------------------------------------

# Crockford base32 alphabet, matching the canonical ULID spec the Rust
# ``ulid`` crate emits. 32 symbols, no I/L/O/U so eyeballing IDs in
# logs is unambiguous.
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    """Encode ``value`` as Crockford-base32 padded to ``length`` chars.

    Internal helper for :func:`_new_ulid`. Big-endian by character.
    """
    out: list[str] = []
    for _ in range(length):
        out.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _new_ulid() -> str:
    """Generate a fresh 26-char ULID.

    48 bits of unix-ms timestamp + 80 bits of OS entropy. We avoid the
    monotonic randomness extension because the resolver only needs
    uniqueness, not per-millisecond ordering. Output is byte-identical
    in shape to what the Rust ``ulid`` crate produces (26 chars,
    Crockford base32, sortable by leading timestamp).
    """
    ts_ms = int(time.time() * 1_000) & ((1 << 48) - 1)
    rand_bits = int.from_bytes(os.urandom(10), "big")
    ts_part = _encode_crockford(ts_ms, 10)
    rand_part = _encode_crockford(rand_bits, 16)
    return ts_part + rand_part


# ---------------------------------------------------------------------------
# UserId
# ---------------------------------------------------------------------------


class UserId(str):
    """Opaque canonical handle for one human.

    ULID-style: 26-character Crockford base32, lexicographically
    sortable. Subclasses ``str`` so it serialises transparently to
    JSON / SQLite TEXT and round-trips through wire boundaries without
    needing a custom encoder — matches the Rust ``#[serde(transparent)]``
    derive.

    Construct via :meth:`UserId.generate` (random) or by wrapping a
    stored id (``UserId("01HV3K...")``). The constructor performs no
    structural validation: callers that read from SQLite always
    receive output from a previous ``generate()``.
    """

    __slots__ = ()

    @classmethod
    def generate(cls) -> UserId:
        """Mint a fresh ULID-backed user id."""
        return cls(_new_ulid())

    def as_str(self) -> str:
        """Return the underlying string. Provided for parity with the
        Rust ``UserId::as_str`` surface; ``str(user_id)`` works too."""
        return str(self)


# ---------------------------------------------------------------------------
# BindingKind
# ---------------------------------------------------------------------------


class BindingKind(StrEnum):
    """How a ``(channel, channel_user_id) → user_id`` binding was established.

    Used by the admin UI to flag whether an alias was auto-bound (low
    confidence), proven via verification (high), or operator-decreed
    (manual override). The string values are the canonical SQLite
    storage shape AND the wire (JSON) shape — keep these in sync with
    the Rust ``BindingKind::as_str``.
    """

    AUTO = "auto"
    """First-seen by ``resolve_or_create``. The resolver minted a new
    ``user_id`` and wrote this row. No proof the human is the same as
    any other ``user_id``."""

    VERIFIED = "verified"
    """Bound via the verification-phrase protocol — the human proved
    they own both ends of the (now-merged) identity."""

    OPERATOR = "operator"
    """Bound by operator decision through ``/admin/identity``."""

    def as_str(self) -> str:
        """SQLite text representation. Stable wire shape; both serde
        and the store impl use this value."""
        return self.value

    @classmethod
    def from_db_str(cls, raw: str) -> BindingKind:
        """Inverse of :meth:`as_str`.

        Unknown strings collapse to :attr:`AUTO` so a forward-compatible
        read of an unknown future variant degrades gracefully rather
        than 500ing. Matches the Rust ``from_db_str`` behaviour.
        """
        if raw == "verified":
            return cls.VERIFIED
        if raw == "operator":
            return cls.OPERATOR
        return cls.AUTO


# ---------------------------------------------------------------------------
# Aliases + phrase records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelAlias:
    """One row in ``user_aliases``.

    The PK is ``(channel, channel_user_id)``, so a single alias maps
    to exactly one ``UserId``; merges work by reattributing rows, not
    duplicating. ``created_at`` is a timezone-aware
    :class:`datetime.datetime` — RFC-3339 at the wire boundary.
    """

    channel: str
    channel_user_id: str
    user_id: UserId
    binding_kind: BindingKind
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VerificationPhrase:
    """One verification phrase issued by an operator and not yet
    redeemed (or expired).

    The store owns the lifecycle: created in
    :meth:`SqliteIdentityStore.issue_phrase`, transitions to
    ``consumed_at = Some(_)`` on redemption, GC'd by a periodic sweep.
    """

    phrase: str
    user_id: UserId
    issued_on_channel: str
    """The channel the phrase was issued *from*. The redemption must
    land on a different channel (the cross-channel proof) to be useful,
    but the protocol allows same-channel redemption for test fixtures."""
    issued_on_channel_user_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class UserSummary:
    """One row in :meth:`IdentityStore.list_users`.

    Wire shape matches the UI's ``UserSummary`` interface (defined
    on the TS side under ``ui/lib/api/identity.ts``).
    """

    user_id: UserId
    display_name: str | None
    alias_count: int
    """Number of aliases bound to this user_id at query time."""


__all__ = [
    "BindingKind",
    "ChannelAlias",
    "UserId",
    "UserSummary",
    "VerificationPhrase",
]
