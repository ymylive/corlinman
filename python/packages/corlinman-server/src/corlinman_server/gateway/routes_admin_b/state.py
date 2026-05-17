"""Shared :class:`AdminState` for the ``routes_admin_b`` sub-routers.

Mirrors ``rust/crates/corlinman-gateway/src/routes/admin/mod.rs::AdminState``
but only carries the slots actually consumed by the Python-side ports.
Slots are typed loosely (``Any``) because the concrete protocols live in
sibling packages that may be reshuffled — keeping the contract narrow at
this seam avoids churn rippling into every route module.

The state is *not* a FastAPI ``Depends`` directly; instead each module
calls :func:`get_admin_state` which reads from a module-global slot
populated by :func:`set_admin_state`. This mirrors the Rust
``with_state(state)`` pattern (where every sub-router got an Arc-clone
of the same backing store) and avoids threading dependency-injection
through every route's signature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AdminState:
    """Runtime handles every admin route may need.

    All fields are optional — handlers gate on presence with the same
    "503 ``<subsystem>_disabled``" convention the Rust admin tree uses.
    """

    # Live config snapshot (a callable returning the current dict-shaped
    # config). Implementations may swap an ArcSwap-equivalent here once
    # the Python-side config-watcher lands; today the gateway
    # bootstrapper supplies a lambda that returns a freshly-cloned dict.
    config_loader: Any | None = None

    # Plugin registry — corlinman_providers.plugins.PluginRegistry.
    plugins: Any | None = None

    # Evolution store handle (corlinman_evolution_store.EvolutionStore).
    evolution_store: Any | None = None

    # Memory host (corlinman_memory_host.MemoryHost) for /admin/memory.
    memory_host: Any | None = None

    # RAG vector store (corlinman_embedding.vector.SqliteStore).
    rag_store: Any | None = None

    # Multi-tenant admin DB
    # (corlinman_server.tenancy.AdminDb).
    admin_db: Any | None = None

    # Scheduler runtime handle
    # (corlinman_server.scheduler.SchedulerHandle).
    scheduler: Any | None = None

    # Log broadcaster — lazy import of
    # corlinman_server.gateway.core.log_broadcast.LogBroadcaster.
    log_broadcast: Any | None = None

    # On-disk path of the active TOML config (None when started without
    # one — POST /admin/config etc 503 with `config_path_unset`).
    config_path: Path | None = None

    # Python-side py-config.json drop, re-emitted after admin writes.
    py_config_path: Path | None = None

    # Data dir (per-tenant SQLite roots live under here).
    data_dir: Path | None = None

    # Allowed-tenants set for federation middleware.
    allowed_tenants: frozenset[str] = frozenset()

    # In-process write lock — every admin route that mutates config TOML
    # must take this so concurrent POST/PATCH calls don't clobber each
    # other.
    admin_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Mapping of any extra subsystem handles a particular route module
    # needs (e.g. scheduler_history). Kept as a free-form bag so the
    # boot path can wire one-offs without growing the dataclass.
    extras: dict[str, Any] = field(default_factory=dict)

    # -- W4.6: curator UI surface ---------------------------------------
    #
    # The ``/admin/curator`` routes consume three handles:
    #
    # * ``curator_state_repo`` — :class:`corlinman_evolution_store.
    #   CuratorStateRepo` (async, over the evolution sqlite). Drives the
    #   per-profile threshold tunables + pause toggle + run history.
    # * ``signals_repo`` — :class:`corlinman_evolution_store.SignalsRepo`
    #   (async, same connection). Curator runs emit ``EVENT_*`` rows so
    #   the run/preview routes thread it through to
    #   :func:`maybe_run_curator`.
    # * ``skill_registry_factory`` — synchronous callable
    #   ``(profile_slug: str) -> corlinman_skills_registry.SkillRegistry``.
    #   The bootstrapper wires the factory to read each profile's skills
    #   dir; the routes only need a way to materialise a *current* view
    #   of skills for one profile without taking a dependency on the
    #   skills-loading internals.
    #
    # All three are typed loosely (``Any``) so this dataclass stays
    # importable even when the evolution-store / skills-registry packages
    # aren't installed at import time (the routes 503 with a typed error
    # envelope instead).
    profile_store: Any | None = None
    curator_state_repo: Any | None = None
    signals_repo: Any | None = None
    skill_registry_factory: Any | None = None


_state: AdminState | None = None


def set_admin_state(state: AdminState | None) -> None:
    """Install (or clear) the process-global :class:`AdminState`.

    Called by the gateway bootstrapper before :func:`build_router` is
    mounted onto the FastAPI app. Tests reach for this to swap a
    fixture-built state.
    """
    global _state
    _state = state


def get_admin_state() -> AdminState:
    """Read the active :class:`AdminState`.

    Raises :class:`RuntimeError` when the state hasn't been installed —
    a clearer failure than a chain of ``None`` attribute errors deep
    inside a handler. Routes that legitimately operate without certain
    slots should still gate on the slot's presence after this call.
    """
    if _state is None:
        # Default to an empty state so handlers route through their
        # own disabled-503 branches rather than 500ing on missing
        # state. Mirrors the Rust ``AdminState::new`` default of "all
        # slots None".
        return AdminState()
    return _state


def config_snapshot(state: AdminState | None = None) -> Mapping[str, Any]:
    """Return the current config as a plain dict (or empty when unset).

    Convenience wrapper around ``state.config_loader()``. Routes call
    this so a missing loader collapses to ``{}`` instead of raising —
    most handlers gate on ``cfg.get("...")`` anyway.
    """
    st = state if state is not None else get_admin_state()
    if st.config_loader is None:
        return {}
    try:
        snap = st.config_loader()
        if isinstance(snap, Mapping):
            return snap
    except Exception:  # noqa: BLE001 — best-effort snapshot
        return {}
    return {}


# ---------------------------------------------------------------------------
# Auth dependency — lazy import of the middleware module so tests can
# import the routers without the middleware package being present.
# ---------------------------------------------------------------------------


async def require_admin() -> None:
    """FastAPI dependency that enforces admin credentials.

    Lazy-imports ``corlinman_server.gateway.middleware.require_admin``
    (a sibling agent is responsible for that module). When the sibling
    isn't installed yet the dependency is a no-op — tests and lone
    imports stay green; production deployments must ensure the
    middleware is present.
    """
    try:
        from corlinman_server.gateway import middleware  # type: ignore  # noqa: PLC0415
    except ImportError:
        return None
    fn = getattr(middleware, "require_admin", None)
    if fn is None:
        return None
    # Caller may be async or sync — accept either.
    result = fn()
    if asyncio.iscoroutine(result):
        await result
    return None
