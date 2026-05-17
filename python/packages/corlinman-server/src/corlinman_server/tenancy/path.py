"""Filesystem-path helpers for per-tenant SQLite layout.

Python port of ``corlinman-tenant::path``.

All per-tenant DBs live under ``<root>/tenants/<tenant>/<name>.sqlite``.
Centralising the layout in one function lets us:

* grep for path leaks at audit time (any ``f"...{name}.sqlite"`` in
  another module is a bug),
* change the layout in one place (e.g. add a hash-prefix shard if a
  single deployment ever sees more tenants than ext4 likes per dir),
* keep ``?tenant=`` query injection from escaping the data dir — the
  :class:`TenantId` slug regex already excludes ``/`` and ``.``, but
  plumbing the path build through :meth:`pathlib.PurePath.joinpath`
  rather than f-strings adds a second layer of "this can't traverse"
  defence.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.tenancy.id import TenantId


def tenant_root_dir(root: Path | str, tenant: TenantId) -> Path:
    """Absolute (or root-relative) path to the directory holding all
    per-tenant data files for ``tenant``. Layout::

        <root>/tenants/<tenant_id>/

    The ``tenants`` subdir is the boundary — every per-tenant file
    (SQLite or otherwise) sits underneath it. Single-tenant legacy
    deployments run as ``<root>/tenants/default/``.
    """
    return Path(root) / "tenants" / tenant.as_str()


def tenant_db_path(root: Path | str, tenant: TenantId, name: str) -> Path:
    """Full path for the per-tenant SQLite file named ``name``, e.g.
    ``tenant_db_path(root, acme, "evolution")`` →
    ``<root>/tenants/acme/evolution.sqlite``.

    ``name`` is taken bare (no ``.sqlite`` suffix) so call-sites read
    like the legacy single-tenant constants
    (``evolution_db_path("evolution")``), and so a future
    ``name = "agent_state.bak"`` couldn't accidentally produce a
    double-suffix path.
    """
    return tenant_root_dir(root, tenant) / f"{name}.sqlite"


__all__ = ["tenant_db_path", "tenant_root_dir"]
