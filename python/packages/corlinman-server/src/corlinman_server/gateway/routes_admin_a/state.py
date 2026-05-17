"""``AdminState`` — shared state for the ``routes_admin_a`` submodule.

Python port of the relevant slice of
``rust/crates/corlinman-gateway/src/routes/admin/mod.rs::AdminState``.

Only the fields the routes in *this* submodule consume are modelled
here. Sibling state pieces (``approval_gate`` for the full Rust
``ApprovalGate``, ``scheduler_history``, ``log_broadcast`` …) belong to
``routes_admin_b`` and are kept out of this dataclass to avoid agent
overreach.

Wiring contract
---------------

Routers in this submodule are *factories* — they take no positional
state argument. State is injected via FastAPI's
``dependency_overrides`` map keyed on :func:`get_admin_state`. The
bootstrapper builds an :class:`AdminState`, stores it via
:func:`set_admin_state`, and every route in this submodule reads back
the same singleton through the ``Depends(get_admin_state)`` shim. This
mirrors the Rust ``Router::with_state`` pattern but plays nicer with
FastAPI's dependency machinery (no per-route ``State<AdminState>``
extractor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corlinman_server.tenancy import AdminDb, TenantId


@dataclass
class AdminState:
    """Shared read-only state surface for ``routes_admin_a``.

    Every field is optional — the routes themselves return a 503 with a
    descriptive ``error`` envelope when a dependency they need isn't
    wired. Tests build a stripped-down state with just the bits the
    route under test needs.
    """

    # -- /admin/agents ------------------------------------------------
    #
    # The Rust side reads ``cfg.server.data_dir``; we let the
    # bootstrapper pass the resolved dir verbatim so the admin slice
    # doesn't have to take a dep on the gateway Config shape.
    data_dir: Path | None = None

    # -- /admin/auth --------------------------------------------------
    #
    # Argon2id password hash + username for the configured admin (the
    # Rust ``cfg.admin.username`` / ``cfg.admin.password_hash`` pair).
    # ``None`` means "no admin configured" — the login route then 503s
    # ``admin_not_configured``.
    admin_username: str | None = None
    admin_password_hash: str | None = None
    # Path of the on-disk config file — used by the onboard + password
    # routes to persist the new hash. ``None`` falls back to in-memory
    # updates only, matching the Rust ``config_path: None`` 503 path.
    config_path: Path | None = None
    # Session TTL in seconds; mirrors the Rust
    # ``AdminSessionStore::ttl``. Defaults to 24h.
    session_ttl_seconds: int = 86_400
    # In-memory session token registry. Built lazily by
    # :func:`build_default_state` — tests construct one explicitly when
    # they need to exercise the cookie path.
    session_store: Any | None = None
    # Serialises the verify-then-write critical section in onboard +
    # password rotation routes. Optional so tests that don't need it
    # don't have to construct it; ``_admin_auth_lock`` in
    # ``auth.py`` falls back to a module-level lock when absent.
    admin_write_lock: Any | None = None

    # -- /admin/approvals --------------------------------------------
    #
    # ``corlinman_providers.plugins.ApprovalStore`` / ``ApprovalQueue``
    # — typed loosely with ``Any`` to keep the dataclass importable
    # without the providers package being installed at import time.
    approval_store: Any | None = None
    approval_queue: Any | None = None

    # -- /admin/api-keys + /admin/tenants ---------------------------
    #
    # Shared ``AdminDb`` handle backing the multi-tenant admin DB.
    admin_db: AdminDb | None = None
    # Operator-allowed tenant set (the Rust ``allowed_tenants``).
    allowed_tenants: set[TenantId] = field(default_factory=set)
    # Whether multi-tenant mode is enabled. ``False`` → tenants /
    # api-keys / sessions routes all return their respective "disabled"
    # 403 / 503 envelopes.
    tenants_enabled: bool = False
    # Default tenant slug — the Rust ``cfg.tenants.default``. Falls
    # back to ``corlinman_server.tenancy.default_tenant()`` when unset.
    default_tenant: TenantId | None = None

    # -- /admin/channels --------------------------------------------
    #
    # Loose shape: the python ``corlinman_channels`` package doesn't
    # ship a single live ``ChannelManager`` analogue yet. We carry a
    # ``ChannelsConfig``-like dict the bootstrapper hands us and a
    # write-back callback the keywords route invokes when the operator
    # mutates the live keywords map. ``None`` for either means "no
    # writable channels surface" → 503.
    channels_config: dict[str, Any] | None = None
    channels_writer: Any | None = None

    # -- /admin/embedding -------------------------------------------
    #
    # Snapshot of the active embedding config + a writer that persists
    # mutations. The Python side keeps the snapshot as a dict so the
    # admin surface doesn't have to take a dep on a specific
    # ``EmbeddingConfig`` shape.
    embedding_config: dict[str, Any] | None = None
    embedding_writer: Any | None = None
    # URL of the python ``embedding/benchmark`` sidecar surface. Only
    # used by the benchmark route's HTTP passthrough; defaults to the
    # localhost address the Rust side already uses.
    py_admin_url: str = "http://127.0.0.1:50052"

    # -- /admin/identity --------------------------------------------
    #
    # ``corlinman_identity.IdentityStore`` (protocol) instance. ``None``
    # → all four /admin/identity routes 503 ``identity_disabled``.
    identity_store: Any | None = None

    # -- /admin/sessions --------------------------------------------
    #
    # Wave-2 kill switch + an optional pre-resolved sessions backend.
    # ``sessions_disabled = True`` → 503 ``sessions_disabled`` on every
    # ``/admin/sessions*`` request. The actual SQLite lookup is done by
    # ``sessions.py`` against ``data_dir`` so this state field is the
    # operator gate, not the data source.
    sessions_disabled: bool = False


# ---------------------------------------------------------------------------
# Singleton plumbing — FastAPI dependency-override surface.
# ---------------------------------------------------------------------------

# Module-level slot. The Rust side uses ``Router::with_state(state)``;
# Python's FastAPI dependency-overrides surface is the closest analogue
# and the convention used elsewhere in this gateway port.
_STATE: AdminState | None = None


def set_admin_state(state: AdminState | None) -> None:
    """Install the shared :class:`AdminState` instance. Tests call this
    with a freshly constructed state per test; production callers call
    it once at boot."""
    global _STATE  # noqa: PLW0603 — module-level singleton is the contract
    _STATE = state


def get_admin_state() -> AdminState:
    """FastAPI dependency: return the currently installed
    :class:`AdminState`.

    Raises :class:`RuntimeError` when nothing has been installed —
    tests that hit a route without calling :func:`set_admin_state`
    first fail loudly so the omission surfaces as a test bug rather
    than an obscure 500.
    """
    if _STATE is None:
        raise RuntimeError(
            "routes_admin_a: AdminState not installed; "
            "call set_admin_state(...) before mounting the router"
        )
    return _STATE


__all__ = [
    "AdminState",
    "get_admin_state",
    "set_admin_state",
]
