"""Port of ``corlinman-tenant::path`` unit tests to pytest."""

from __future__ import annotations

from pathlib import Path

from corlinman_server.tenancy import TenantId, tenant_db_path, tenant_root_dir


def test_root_dir_under_tenants_subdir() -> None:
    root = Path("/data")
    tenant = TenantId.new("acme")
    assert tenant_root_dir(root, tenant) == Path("/data/tenants/acme")


def test_db_path_appends_sqlite_suffix_once() -> None:
    root = Path("/data")
    tenant = TenantId.new("acme")
    assert tenant_db_path(root, tenant, "evolution") == Path("/data/tenants/acme/evolution.sqlite")
    assert tenant_db_path(root, tenant, "kb") == Path("/data/tenants/acme/kb.sqlite")


def test_legacy_default_layout_is_predictable() -> None:
    # Single-tenant compat: the legacy data-dir layout (e.g. a
    # sibling worktree built before Phase 4) becomes
    # `<root>/tenants/default/<name>.sqlite`. Migration from the
    # pre-Phase-4 flat layout is a separate boot step.
    root = Path("/data")
    tenant = TenantId.legacy_default()
    assert tenant_db_path(root, tenant, "evolution") == Path(
        "/data/tenants/default/evolution.sqlite"
    )


def test_relative_root_works_for_tests() -> None:
    # Integration tests pass an absolute tempdir, but unit tests /
    # fixtures sometimes pass `Path(".")`.
    root = Path(".")
    tenant = TenantId.new("acme")
    p = tenant_db_path(root, tenant, "evolution")
    # The join is structural — the leading `.` is preserved.
    assert str(p).endswith("tenants/acme/evolution.sqlite")


def test_root_accepts_str_or_pathlike() -> None:
    """Convenience: the API takes either a :class:`pathlib.Path` or
    a string. Both must produce the same final path."""
    tenant = TenantId.new("acme")
    p_from_path = tenant_db_path(Path("/data"), tenant, "evolution")
    p_from_str = tenant_db_path("/data", tenant, "evolution")
    assert p_from_path == p_from_str
