"""In-memory admin session store.

Python port of ``rust/crates/corlinman-gateway/src/middleware/admin_session.rs``.

Same role: gives ``/admin/login`` somewhere to park a token after argon2
succeeds, and lets :class:`~corlinman_server.gateway.middleware.admin_auth.AdminAuthMiddleware`
validate a ``Cookie: corlinman_session=<token>`` instead of asking the
browser to resend Basic credentials on every request.

Design:
  * dict keyed by a random UUID token, guarded by a ``threading.Lock``
    (the equivalent of Rust's ``DashMap`` shard locks). The store is
    accessed from middleware *and* admin handlers concurrently, so a
    plain dict isn't safe under threading even though we mostly live
    in a single asyncio loop.
  * Sessions expire after ``ttl`` since ``last_used``; a background
    asyncio task calls :meth:`gc` every ``ttl / 4`` (min 60s).
  * No persistence across restart — operators log in again.

Security posture mirrors the Rust port:
  * Token is a v4 UUID (~122 bits of entropy).
  * :meth:`validate` returns a copy so callers can't keep a live
    reference into the inner dict.
  * ``last_used`` is bumped on every successful validate so an active
    session keeps sliding forward.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AdminSession:
    """One authenticated admin session. Frozen so cloned copies handed
    to callers can't mutate the stored state from a distance."""

    user: str
    created_at: datetime
    last_used: datetime


class AdminSessionStore:
    """Thread-safe in-memory session registry.

    Cloneable handle — share across middleware + login/logout handlers
    by passing the same instance. The asyncio GC task can be started
    optionally; tests just call :meth:`gc` directly.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, AdminSession] = {}
        self._lock = threading.Lock()
        self._gc_task: asyncio.Task[None] | None = None
        self._gc_stop: asyncio.Event | None = None

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    # ---- core API ----------------------------------------------------------

    def create(self, user: str) -> str:
        """Issue a fresh token for ``user`` and park the session.
        Returns the opaque token the cookie carries."""

        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._lock:
            self._sessions[token] = AdminSession(
                user=user, created_at=now, last_used=now
            )
        return token

    def validate(self, token: str) -> AdminSession | None:
        """Look up ``token`` and bump its ``last_used``. ``None`` means
        the token is unknown or has expired (and was evicted as a side
        effect)."""

        now = datetime.now(timezone.utc)
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            elapsed = (now - entry.last_used).total_seconds()
            if elapsed > self._ttl_seconds:
                # Inline expiry sweep — matches Rust's
                # validate-also-evicts semantics.
                self._sessions.pop(token, None)
                return None
            # Touch the slot before handing the clone back out.
            refreshed = replace(entry, last_used=now)
            self._sessions[token] = refreshed
            return refreshed

    def invalidate(self, token: str) -> None:
        """Drop a token unconditionally. Called by ``/admin/logout``."""

        with self._lock:
            self._sessions.pop(token, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._sessions

    def gc(self) -> int:
        """Evict every entry whose ``last_used`` is older than ``ttl``.

        Returns the number of sessions removed. Exposed so tests can
        exercise the sweep directly without waiting on the background
        task's tick."""

        now = datetime.now(timezone.utc)
        ttl = self._ttl_seconds
        with self._lock:
            victims = [
                token
                for token, session in self._sessions.items()
                if (now - session.last_used).total_seconds() > ttl
            ]
            for token in victims:
                self._sessions.pop(token, None)
        return len(victims)

    # ---- background GC task -----------------------------------------------

    def start_gc(self) -> asyncio.Task[None]:
        """Spawn the GC sweep task. Tick interval is ``ttl / 4`` clamped
        to a 60-second minimum so a 0-TTL test setup doesn't spin."""

        if self._gc_task is not None and not self._gc_task.done():
            return self._gc_task
        loop = asyncio.get_event_loop()
        self._gc_stop = asyncio.Event()
        interval = max(self._ttl_seconds / 4.0, 60.0)
        self._gc_task = loop.create_task(
            self._gc_loop(interval), name="gateway.admin_session.gc"
        )
        return self._gc_task

    async def stop_gc(self) -> None:
        """Cancel the GC task and await its exit."""

        if self._gc_stop is not None:
            self._gc_stop.set()
        task = self._gc_task
        if task is None:
            return
        self._gc_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _gc_loop(self, interval: float) -> None:
        assert self._gc_stop is not None
        try:
            while not self._gc_stop.is_set():
                try:
                    await asyncio.wait_for(self._gc_stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                if self._gc_stop.is_set():
                    return
                evicted = self.gc()
                if evicted:
                    logger.debug("admin_session.gc.swept", evicted=evicted)
        except asyncio.CancelledError:
            return


__all__ = ["AdminSession", "AdminSessionStore"]
