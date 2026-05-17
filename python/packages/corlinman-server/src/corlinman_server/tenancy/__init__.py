"""``corlinman_server.tenancy`` — Python port of the ``corlinman-tenant`` crate.

Multi-tenancy primitives for the Python AI plane:

* :class:`TenantId` — slug-shaped newtype with validation
  (``^[a-z][a-z0-9-]{0,62}$``), JSON-friendly serialisation, and the
  reserved ``"default"`` value for legacy single-tenant boots.
* :func:`tenant_db_path` / :func:`tenant_root_dir` — single source of
  truth for the per-tenant SQLite layout
  (``<root>/tenants/<tenant>/<name>.sqlite``).
* :class:`TenantPool` — multi-DB :mod:`aiosqlite` connection wrapper
  keyed by ``(TenantId, db_name)``. Lazy-opens each per-tenant
  connection on first use, caches it, and hands it back to downstream
  stores.
* :class:`AdminDb` — schema + thin CRUD wrapper for the root-level
  ``tenants.sqlite`` admin DB (tenants, admins, federation peers,
  api keys).

This module is intentionally thin: it does **not** know any of the
schemas it stores — each downstream module keeps its own
``SCHEMA_SQL`` + idempotent ALTER block. :class:`TenantPool` only
manages the ``(tenant, db_name) -> connection`` map.
"""

from __future__ import annotations

from corlinman_server.tenancy.admin_schema import (
    SCHEMA_SQL,
    AdminDb,
    AdminDbConnectError,
    AdminDbError,
    AdminExistsError,
    AdminRow,
    ApiKeyRow,
    FederationPeer,
    MintedApiKey,
    TenantExistsError,
    TenantRow,
    hash_api_key_token,
)
from corlinman_server.tenancy.id import (
    DEFAULT_TENANT_ID,
    TENANT_SLUG_REGEX_STR,
    TenantId,
    TenantIdEmpty,
    TenantIdError,
    TenantIdInvalidShape,
    default_tenant,
)
from corlinman_server.tenancy.path import tenant_db_path, tenant_root_dir
from corlinman_server.tenancy.pool import (
    TenantPool,
    TenantPoolConnectError,
    TenantPoolCreateDirError,
    TenantPoolError,
)

__all__ = [
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "TENANT_SLUG_REGEX_STR",
    "AdminDb",
    "AdminDbConnectError",
    "AdminDbError",
    "AdminExistsError",
    "AdminRow",
    "ApiKeyRow",
    "FederationPeer",
    "MintedApiKey",
    "TenantExistsError",
    "TenantId",
    "TenantIdEmpty",
    "TenantIdError",
    "TenantIdInvalidShape",
    "TenantPool",
    "TenantPoolConnectError",
    "TenantPoolCreateDirError",
    "TenantPoolError",
    "TenantRow",
    "default_tenant",
    "hash_api_key_token",
    "tenant_db_path",
    "tenant_root_dir",
]
