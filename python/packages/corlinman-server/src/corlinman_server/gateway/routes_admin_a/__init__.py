"""``routes_admin_a`` — Python port of half of the Rust admin route tree.

Mirrors these Rust modules from
``rust/crates/corlinman-gateway/src/routes/admin/``:

* :mod:`agents`     — markdown agent file CRUD (``/admin/agents*``)
* :mod:`api_keys`   — bearer-token mint (``/admin/api_keys*``)
* :mod:`approvals`  — tool-approval queue (``/admin/approvals*``)
* :mod:`auth`       — admin login / session lifecycle
                      (``/admin/login``, ``/admin/logout``, etc.)
* :mod:`channels`   — QQ/OneBot channel mgmt (``/admin/channels/qq*``)
* :mod:`embedding`  — embedding provider + benchmark (``/admin/embedding*``)
* :mod:`identity`   — identity graph admin (``/admin/identity*``)
* :mod:`sessions`   — replay surface (``/admin/sessions*``)
* :mod:`tenants`    — multi-tenant registry (``/admin/tenants*``)

Each submodule exposes a ``router()`` factory returning a configured
:class:`fastapi.APIRouter`. State is plumbed via FastAPI's
dependency-override surface keyed on :func:`get_admin_state`; the
bootstrapper installs the shared :class:`AdminState` once via
:func:`set_admin_state` and every route reads it back through the
``Depends(get_admin_state)`` shim.

The composed parent router is :func:`build_router`. It mirrors
:func:`corlinman_gateway::routes::admin::router_with_state` on the
Rust side, minus the auth + tenant-scope middleware (those layers are
the bootstrapper's responsibility). Each sub-router already declares
its own ``/admin/...`` prefixes, so :func:`build_router` mounts them
under the root with no extra prefix.

Admin auth is plumbed via :func:`_auth_shim.require_admin_dependency` —
a lazy import that falls through as a no-op when
``corlinman_server.gateway.middleware.admin_auth`` isn't installed
yet (parallel agent work-in-progress).
"""

from __future__ import annotations

from fastapi import APIRouter

from corlinman_server.gateway.routes_admin_a import (
    agents as _agents,
    api_keys as _api_keys,
    approvals as _approvals,
    auth as _auth,
    channels as _channels,
    embedding as _embedding,
    identity as _identity,
    sessions as _sessions,
    tenants as _tenants,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
    set_admin_state,
)

__all__ = [
    "AdminState",
    "build_router",
    "get_admin_state",
    "router",
    "set_admin_state",
]


def build_router() -> APIRouter:
    """Compose every admin-A sub-router into one parent APIRouter.

    Mirrors :func:`corlinman_gateway::routes::admin::router_with_state`
    on the Rust side (modulo the auth/tenant-scope middleware which
    the bootstrapper installs separately).
    """
    root = APIRouter()
    for mod in (
        _agents,
        _api_keys,
        _approvals,
        _auth,
        _channels,
        _embedding,
        _identity,
        _sessions,
        _tenants,
    ):
        root.include_router(mod.router())
    return root


# Alias — matches the parallel `routes_admin_b.build_router` naming
# while also providing a shorter spelling for call sites that already
# import :func:`router` from sibling FastAPI surfaces.
router = build_router
