"""Lightweight tenant scoping shim.

Mirrors the small slice of the Rust ``corlinman-tenant`` surface that
``corlinman-identity`` actually consumes:

* a ``TenantId`` wrapper around a slug (``"default"``, ``"acme"``, ...)
* the ``legacy_default()`` helper, which other per-tenant Python stores
  use to map onto the pre-Phase-4 single-tenant layout
* ``tenant_db_path``, which puts the SQLite file under
  ``<data_dir>/tenants/<slug>/<db>.sqlite`` for named tenants and falls
  back to ``<data_dir>/<db>.sqlite`` for the legacy default

Why this lives here instead of importing from
``corlinman_server.tenancy``: that module is being authored concurrently
by another agent and the exact import path / type shape isn't pinned
yet. Accepting any value matching :class:`TenantIdLike` at API
boundaries lets the canonical implementation slot in later without a
breaking change here.

TODO(tenancy-integration): once ``corlinman_server.tenancy`` (or the
authoritative ``corlinman-tenant`` Python package) ships, re-export
``TenantId`` from there and delete the local class — or keep this as a
thin adapter that wraps the canonical type. The on-disk path layout
must stay byte-identical with the Rust ``corlinman-tenant`` crate
(``tenant_db_path``) so the Python and Rust stores can read the same
files.
"""

from __future__ import annotations

from pathlib import Path
from typing import NewType, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Type vocabulary
# ---------------------------------------------------------------------------

# Opaque slug. ``NewType`` keeps it a plain ``str`` at runtime (so
# ``TenantId("acme")`` works) but lets type-checkers distinguish it
# from arbitrary user-supplied strings at API boundaries.
TenantId = NewType("TenantId", str)

# Sentinel for the pre-Phase-4 single-tenant layout. Matches the Rust
# ``TenantId::legacy_default()`` slug so SQLite paths line up.
LEGACY_DEFAULT_SLUG = "default"


@runtime_checkable
class TenantIdLike(Protocol):
    """Structural type for anything that resolves to a tenant slug.

    Accepts the local :data:`TenantId` ``NewType`` (which is ``str``),
    bare strings, and any future tenant object that exposes a ``slug``
    attribute. The store boundary calls :func:`tenant_slug` to flatten
    whatever was passed into a plain ``str``.
    """

    # No required members — we duck-type via :func:`tenant_slug`. The
    # ``Protocol`` exists so static type checkers see a single shared
    # name at API boundaries.


def legacy_default() -> TenantId:
    """Sentinel ``TenantId`` for the legacy single-tenant layout.

    Mirrors ``corlinman_tenant::TenantId::legacy_default()`` on the
    Rust side. Used by tests and by callers that haven't been threaded
    a real tenant yet.
    """
    return TenantId(LEGACY_DEFAULT_SLUG)


def tenant_slug(tenant: TenantIdLike | str) -> str:
    """Extract the tenant slug from any tenant-like value.

    Accepts:

    * bare strings (``"default"``, ``"acme"``)
    * the local :data:`TenantId` ``NewType`` (also a string at runtime)
    * any object exposing a ``slug`` attribute — anticipates the
      canonical ``corlinman_server.tenancy.TenantId`` once it lands

    Empty input collapses to the legacy default rather than raising —
    matches the Rust ``TenantId::new`` fallback shape and keeps the
    boundary forgiving for callers that haven't been wired through
    yet.
    """
    if isinstance(tenant, str):
        return tenant or LEGACY_DEFAULT_SLUG
    slug = getattr(tenant, "slug", None)
    if isinstance(slug, str) and slug:
        return slug
    return LEGACY_DEFAULT_SLUG


def is_legacy_default(tenant: TenantIdLike | str) -> bool:
    """True iff ``tenant`` should map onto the unscoped legacy path layout."""
    return tenant_slug(tenant) == LEGACY_DEFAULT_SLUG


def tenant_db_path(data_dir: Path, tenant: TenantIdLike | str, db_name: str) -> Path:
    """``<data_dir>/tenants/<slug>/<db>.sqlite`` for named tenants.

    For the legacy default tenant the path collapses to
    ``<data_dir>/<db>.sqlite`` — preserves the pre-Phase-4 single-tenant
    layout the rest of the gateway uses. Byte-identical with the Rust
    ``corlinman_tenant::tenant_db_path`` so a Python and a Rust process
    pointed at the same data dir read the same files.
    """
    filename = f"{db_name}.sqlite"
    if is_legacy_default(tenant):
        return data_dir / filename
    return data_dir / "tenants" / tenant_slug(tenant) / filename


__all__ = [
    "LEGACY_DEFAULT_SLUG",
    "TenantId",
    "TenantIdLike",
    "is_legacy_default",
    "legacy_default",
    "tenant_db_path",
    "tenant_slug",
]
