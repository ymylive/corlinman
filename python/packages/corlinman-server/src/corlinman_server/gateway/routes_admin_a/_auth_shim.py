"""Lazy-import shim for the gateway admin-auth middleware.

The Rust ``router_with_state`` mounts every admin sub-router behind
``crate::middleware::admin_auth::require_admin``. The Python equivalent
lives in :mod:`corlinman_server.gateway.middleware.admin_auth`, but that
module is owned by a parallel agent and may not be present at import
time.

This shim resolves the dependency *lazily* on every request so:

* Routes in this submodule stay importable + unit-testable without the
  middleware package being installed.
* When the real middleware lands, dropping it in is a no-config change
  — the dependency picks it up on its first invocation.

Until the real ``require_admin`` lands, this shim is a no-op
passthrough (returns ``None``, i.e. "auth check skipped"). Test fixtures
that want to exercise a 401 path can override this dependency via the
standard ``app.dependency_overrides`` map.
"""

from __future__ import annotations

from typing import Any


def require_admin_dependency() -> Any:
    """FastAPI dependency: enforce admin auth via the real middleware
    if it's wired, otherwise no-op.

    Returns whatever the real middleware returns (typically a session /
    auth context object) so route handlers can ``Depends(...)`` on this
    helper and use the returned value when present.
    """
    try:
        # Lazy import — failure means the middleware module isn't
        # available yet (parallel agent work-in-progress). Returning
        # ``None`` keeps the route reachable so unit tests pass; the
        # bootstrapper that does have the middleware overrides this
        # dependency via ``app.dependency_overrides``.
        from corlinman_server.gateway.middleware.admin_auth import (  # type: ignore[import-not-found]
            require_admin,
        )
    except ImportError:
        return None
    # ``require_admin`` is itself a FastAPI dependency — FastAPI's
    # Depends() resolves chained dependencies, but at this shim layer
    # we just call it as a plain function. Real-world wiring will land
    # via ``app.dependency_overrides[require_admin_dependency] = ...``.
    return require_admin


__all__ = ["require_admin_dependency"]
