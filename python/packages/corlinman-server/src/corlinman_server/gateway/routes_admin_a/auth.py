"""``/admin/login``, ``/admin/logout``, ``/admin/me``,
``/admin/onboard``, ``/admin/password`` — session lifecycle + admin
credential rotation.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/auth.rs``.

These routes mount **outside** the ``require_admin`` middleware on the
Rust side — each handler does its own credential check (argon2 verify
or cookie validate) so the chicken-and-egg "you need a cookie to set
your first cookie" problem doesn't apply. The Python port preserves
that pattern; the router built by :func:`router` does **not** depend
on :func:`require_admin_dependency`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import threading
from pathlib import Path
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
    extract_cookie,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# Minimum length operators must use when picking the admin password.
MIN_PASSWORD_LEN = 8

# Default idle TTL for admin sessions (24 hours). Mirrors
# ``DEFAULT_SESSION_TTL_SECS`` on the Rust side.
DEFAULT_SESSION_TTL_SECS = 86_400


# ``argon2-cffi`` is the shared hashing implementation already pinned
# in the server package's deps. Constructed once at module import time
# so we don't pay the parameter setup cost per call.
_HASHER = PasswordHasher()

# Module-level fallback lock used by the onboard + password routes when
# the AdminState doesn't carry one. Both routes hold it across the
# precondition-check + atomic write so a racing sibling sees the
# winner's state.
_FALLBACK_ADMIN_WRITE_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_in: int


class MeResponse(BaseModel):
    user: str
    created_at: str
    expires_at: str


class OnboardRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a plaintext password with argon2id. Wrapper around
    :class:`PasswordHasher` so the call-sites all agree on the
    instance + params."""
    return _HASHER.hash(password)


def argon2_verify(password: str, encoded: str) -> bool:
    """Constant-time verify of ``password`` against an argon2 PHC
    string. Returns ``False`` on any mismatch (including malformed
    encodings) — matches the Rust ``argon2_verify`` contract."""
    try:
        return _HASHER.verify(encoded, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash / wrong algorithm — treat as mismatch rather
        # than 500. The Rust side does the same via the typed
        # ``PasswordHash::new`` error returning false.
        return False


def _set_cookie_header(token: str, max_age_seconds: int) -> str:
    """Build the ``Set-Cookie`` header value matching the Rust
    ``set_cookie_header`` — ``HttpOnly``, ``SameSite=Strict``,
    ``Path=/``, no ``Secure`` flag (TLS terminates upstream)."""
    return (
        f"{SESSION_COOKIE_NAME}={token}; "
        f"HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age_seconds}"
    )


def _clear_cookie_header() -> str:
    """``Set-Cookie`` header value that clears the session cookie."""
    return (
        f"{SESSION_COOKIE_NAME}=; "
        f"HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    )


def _iso(dt: _dt.datetime) -> str:
    """RFC-3339 / ISO-8601 UTC string."""
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_session_store(state: AdminState) -> AdminSessionStore:
    """Return the active session store, creating a default one when the
    bootstrapper didn't pre-build one. We **mutate** the state so
    every route sees the same store — equivalent to the Rust side
    handing one ``Arc`` around."""
    store = state.session_store
    if store is None:
        store = AdminSessionStore(state.session_ttl_seconds)
        state.session_store = store
    if not isinstance(store, AdminSessionStore):
        # Bootstrapper handed us a foreign session-store impl. Trust
        # it — the test harness may swap in a mock. Caller is on the
        # hook for the API shape.
        return store  # type: ignore[return-value]
    return store


def _read_session_cookie(request: Request) -> str | None:
    """Extract the session cookie from the incoming request."""
    # FastAPI's ``request.cookies`` already parses the header; fall
    # back to the raw header parsing for tests that build a Request
    # directly without going through Starlette's cookie middleware.
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    raw = request.headers.get("cookie")
    if raw is None:
        return None
    return extract_cookie(raw, SESSION_COOKIE_NAME)


def _service_unavailable(error: str, message: str | None = None) -> HTTPException:
    payload: dict[str, Any] = {"error": error}
    if message is not None:
        payload["message"] = message
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload
    )


def _unauthorized(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": error}
    )


async def _atomic_write(path: Path, contents: str) -> None:
    """Async-friendly atomic write: ``<path>.new`` then ``os.replace``.
    The file IO itself is synchronous (the bytes are tiny — admin
    config rather than streaming data), but we offload to a thread
    so the event loop stays free."""

    def _do() -> None:
        parent = path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(contents, encoding="utf-8")
        import os as _os

        _os.replace(tmp, path)

    await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for the session + credential-rotation endpoints.

    **Not** wrapped in the admin-auth dependency — each handler does
    its own credential / cookie check inline."""
    r = APIRouter()

    @r.post(
        "/admin/login",
        response_model=LoginResponse,
        summary="Issue a session cookie",
    )
    async def login(
        body: LoginRequest,
        response: Response,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> LoginResponse:
        if state.admin_username is None or state.admin_password_hash is None:
            raise _service_unavailable("admin_not_configured")

        if body.username != state.admin_username or not argon2_verify(
            body.password, state.admin_password_hash
        ):
            raise _unauthorized("invalid_credentials")

        store = _ensure_session_store(state)
        token = store.create(body.username)
        max_age = store.ttl_seconds() if hasattr(store, "ttl_seconds") else state.session_ttl_seconds

        response.headers["set-cookie"] = _set_cookie_header(token, max_age)
        return LoginResponse(token=token, expires_in=max_age)

    @r.post(
        "/admin/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Clear the session cookie",
    )
    async def logout(
        request: Request,
        response: Response,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        token = _read_session_cookie(request)
        if token and state.session_store is not None:
            try:
                state.session_store.invalidate(token)
            except Exception:
                # Best-effort — the cookie clear below still happens.
                pass
        # 204 NO_CONTENT must not have a body; build the response
        # explicitly so FastAPI doesn't append JSON null.
        out = Response(status_code=status.HTTP_204_NO_CONTENT)
        out.headers["set-cookie"] = _clear_cookie_header()
        return out

    @r.get(
        "/admin/me",
        response_model=MeResponse,
        summary="Inspect the current session",
    )
    async def me(
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MeResponse:
        if state.session_store is None:
            raise _unauthorized("unauthenticated")
        token = _read_session_cookie(request)
        if token is None:
            raise _unauthorized("unauthenticated")
        session = state.session_store.validate(token)
        if session is None:
            raise _unauthorized("session_expired")
        ttl = (
            state.session_store.ttl()
            if hasattr(state.session_store, "ttl")
            else _dt.timedelta(seconds=state.session_ttl_seconds)
        )
        expires_at = session.last_used + ttl
        return MeResponse(
            user=session.user,
            created_at=_iso(session.created_at),
            expires_at=_iso(expires_at),
        )

    @r.post("/admin/onboard", summary="First-run admin bootstrap")
    async def onboard(
        body: OnboardRequest,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        username = body.username.strip()
        if not username:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": "username must be non-empty",
                },
            )
        if len(body.password) < MIN_PASSWORD_LEN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "weak_password",
                    "message": (
                        f"password must be at least {MIN_PASSWORD_LEN} characters"
                    ),
                },
            )

        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            if state.admin_username is not None or state.admin_password_hash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "already_onboarded",
                        "message": (
                            "admin credentials are already configured; "
                            "use POST /admin/password to rotate"
                        ),
                    },
                )
            await _persist_admin_credentials(state, username, body.password)
        return {"status": "ok"}

    @r.post("/admin/password", summary="Rotate the admin password")
    async def change_password(
        body: ChangePasswordRequest,
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        if state.session_store is None:
            raise _service_unavailable("session_store_missing")
        token = _read_session_cookie(request)
        session = (
            state.session_store.validate(token) if token else None
        )
        if session is None:
            raise _unauthorized("unauthenticated")

        lock = state.admin_write_lock or _FALLBACK_ADMIN_WRITE_LOCK
        async with _lock_async(lock):
            if state.admin_username is None or state.admin_password_hash is None:
                raise _service_unavailable("admin_not_configured")
            if session.user != state.admin_username:
                raise _unauthorized("session_user_mismatch")
            if not argon2_verify(body.old_password, state.admin_password_hash):
                raise _unauthorized("invalid_old_password")
            if len(body.new_password) < MIN_PASSWORD_LEN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "weak_password",
                        "message": (
                            f"password must be at least {MIN_PASSWORD_LEN} characters"
                        ),
                    },
                )
            await _persist_admin_credentials(
                state, state.admin_username, body.new_password
            )
        return {"status": "ok"}

    return r


async def _persist_admin_credentials(
    state: AdminState, username: str, plaintext_password: str
) -> None:
    """Hash, swap in-memory snapshot, and (when ``config_path`` is set)
    flush to disk. Mirrors the Rust ``persist_admin_credentials`` helper.

    Raises an :class:`HTTPException` on any unrecoverable failure so the
    handler can surface it directly."""
    try:
        hashed = hash_password(plaintext_password)
    except Exception as exc:  # pragma: no cover — argon2 hash rarely fails
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "hash_failed", "message": str(exc)},
        ) from exc

    # Mutate the in-memory snapshot first so subsequent requests see
    # the new credentials even if the disk write fails (matches the
    # Rust ``state.config.store(...)`` + ``rewrite_py_config`` order).
    state.admin_username = username
    state.admin_password_hash = hashed

    if state.config_path is None:
        # No on-disk config to update — mirrors the Rust 503 only if the
        # *caller* expects a persisted state, otherwise we just leave
        # the in-memory snapshot updated. The Rust handler 503s when
        # config_path is None; we match that contract.
        raise _service_unavailable(
            "config_path_unset",
            "gateway booted without a config file path",
        )

    try:
        # The Python port writes a minimal ``[admin]`` block. The Rust
        # side round-trips the full ``Config`` TOML; reproducing that
        # would require a dep on the Rust config schema. We persist a
        # small TOML fragment that's safe to merge into the operator's
        # config.toml by the bootstrapper. The exact file layout is
        # the integration TODO documented in the submodule README.
        toml_text = (
            f"[admin]\n"
            f'username = "{_toml_escape(username)}"\n'
            f'password_hash = "{_toml_escape(hashed)}"\n'
        )
        await _atomic_write(state.config_path, toml_text)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "write_failed", "message": str(exc)},
        ) from exc


def _toml_escape(s: str) -> str:
    """Minimal TOML-string escape for the two fields we serialise."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# ``asyncio.Lock`` / ``threading.Lock`` dual-mode async context manager.
# ---------------------------------------------------------------------------


class _LockAsyncCM:
    """Awaitable lock CM that works with either ``asyncio.Lock`` or
    ``threading.Lock``. The Rust side uses ``tokio::sync::Mutex``; the
    Python port accepts either kind so tests that pre-build the lock
    via the state dataclass don't have to know which flavor to pass."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._kind: str = "noop"

    async def __aenter__(self) -> None:
        lock = self._lock
        if hasattr(lock, "acquire") and asyncio.iscoroutinefunction(lock.acquire):
            await lock.acquire()
            self._kind = "asyncio"
        elif isinstance(lock, threading.Lock):
            await asyncio.to_thread(lock.acquire)
            self._kind = "thread"
        else:
            # Unknown lock shape — best effort: try ``__aenter__``.
            if hasattr(lock, "__aenter__"):
                await lock.__aenter__()
                self._kind = "ctx"
            elif hasattr(lock, "__enter__"):
                await asyncio.to_thread(lock.__enter__)
                self._kind = "sync_ctx"
            else:
                self._kind = "noop"

    async def __aexit__(self, *exc: Any) -> None:
        lock = self._lock
        if self._kind == "asyncio":
            lock.release()
        elif self._kind == "thread":
            lock.release()
        elif self._kind == "ctx":
            await lock.__aexit__(*exc)
        elif self._kind == "sync_ctx":
            await asyncio.to_thread(lock.__exit__, *exc)


def _lock_async(lock: Any) -> _LockAsyncCM:
    return _LockAsyncCM(lock)


__all__ = [
    "DEFAULT_SESSION_TTL_SECS",
    "MIN_PASSWORD_LEN",
    "ChangePasswordRequest",
    "LoginRequest",
    "LoginResponse",
    "MeResponse",
    "OnboardRequest",
    "argon2_verify",
    "hash_password",
    "router",
]
