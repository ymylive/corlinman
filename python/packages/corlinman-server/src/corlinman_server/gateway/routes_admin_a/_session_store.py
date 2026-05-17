"""In-process admin session-cookie store.

Python port of ``rust/crates/corlinman-gateway/src/middleware/admin_session.rs``
(the minimal slice ``routes/admin/auth.rs`` consumes).

Session tokens are opaque UUIDs minted on ``POST /admin/login`` and
parked in an in-memory ``{token: SessionRow}`` map until either the
operator hits ``POST /admin/logout`` or the idle TTL elapses. The
store is intentionally **not** durable — the Rust side keeps the same
shape; sessions don't survive a gateway restart.
"""

from __future__ import annotations

import datetime as _dt
import threading
import uuid
from dataclasses import dataclass

SESSION_COOKIE_NAME = "corlinman_session"


@dataclass
class SessionRow:
    """One row in :class:`AdminSessionStore`. Mirrors the Rust
    ``Session`` struct: ``user`` (admin username), ``created_at``
    (wall-clock UTC at mint time), ``last_used`` (refreshed on every
    successful ``validate``)."""

    user: str
    created_at: _dt.datetime
    last_used: _dt.datetime


class AdminSessionStore:
    """In-memory session token registry.

    Backed by a ``dict`` guarded by a :class:`threading.Lock`. We use
    ``threading`` rather than ``asyncio`` because the Rust source uses
    a sync ``Mutex`` here and the operations are O(1) hash lookups;
    contention isn't a real concern.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = _dt.timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, SessionRow] = {}
        self._lock = threading.Lock()

    # ---- lifecycle accessors ----------------------------------------

    def ttl(self) -> _dt.timedelta:
        """Session idle TTL."""
        return self._ttl

    def ttl_seconds(self) -> int:
        """Idle TTL as integer seconds (Set-Cookie ``Max-Age`` value)."""
        return int(self._ttl.total_seconds())

    def __len__(self) -> int:  # noqa: D401 — len() proxy
        with self._lock:
            return len(self._sessions)

    # ---- mutation ---------------------------------------------------

    def create(self, user: str) -> str:
        """Mint a fresh token for ``user``. Returns the opaque token
        string the caller stamps into the ``Set-Cookie`` header."""
        token = uuid.uuid4().hex
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        with self._lock:
            self._sessions[token] = SessionRow(
                user=user, created_at=now, last_used=now
            )
        return token

    def invalidate(self, token: str) -> None:
        """Drop the row for ``token`` if it exists. No-op otherwise."""
        with self._lock:
            self._sessions.pop(token, None)

    def validate(self, token: str) -> SessionRow | None:
        """Return the session row when ``token`` is valid (known +
        not idle-expired), refreshing ``last_used``. Returns ``None``
        when the token is unknown or its idle TTL has passed.

        Mirrors the Rust ``validate`` slice — idle expiry is a soft
        eviction; the row is removed from the map so future calls see
        it as unknown.
        """
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        with self._lock:
            row = self._sessions.get(token)
            if row is None:
                return None
            if now - row.last_used > self._ttl:
                # Idle-expired — drop the row so subsequent calls 401.
                self._sessions.pop(token, None)
                return None
            row.last_used = now
            return SessionRow(
                user=row.user, created_at=row.created_at, last_used=row.last_used
            )


def extract_cookie(cookie_header: str, name: str) -> str | None:
    """Pull a single named cookie value from a raw ``Cookie:`` header.

    Mirrors the Rust ``extract_cookie`` helper — splits on ``;`` then
    on ``=`` and looks for the named key. Returns ``None`` when the
    cookie isn't present."""
    for part in cookie_header.split(";"):
        kv = part.strip()
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return None


__all__ = [
    "SESSION_COOKIE_NAME",
    "AdminSessionStore",
    "SessionRow",
    "extract_cookie",
]
