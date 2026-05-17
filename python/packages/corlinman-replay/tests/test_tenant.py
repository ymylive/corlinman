"""Tenant slug validation tests.

The Python sibling keeps its own :class:`TenantId` newtype rather than
coupling to a sibling package's definition. Validation must match the
Rust regex byte-for-byte; these tests are the contract anchor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_replay import (
    DEFAULT_TENANT_ID,
    TenantId,
    TenantIdError,
    tenant_db_path,
    tenant_root_dir,
)


def test_legacy_default_is_valid() -> None:
    t = TenantId.legacy_default()
    assert t.as_str() == "default"
    assert t.is_legacy_default()


def test_default_tenant_id_constant() -> None:
    assert DEFAULT_TENANT_ID == "default"
    assert TenantId.new(DEFAULT_TENANT_ID).is_legacy_default()


@pytest.mark.parametrize(
    "ok",
    [
        "default",
        "acme",
        "acme-corp",
        "tenant1",
        "a",
        "ymylive-prod",
        # 63 chars: max allowed.
        "a234567890123456789012345678901234567890123456789012345678901bc",
    ],
)
def test_accepts_valid_slugs(ok: str) -> None:
    t = TenantId.from_str(ok)
    assert t.as_str() == ok


def test_rejects_empty() -> None:
    with pytest.raises(TenantIdError, match="must not be empty"):
        TenantId.new("")


@pytest.mark.parametrize(
    "bad",
    [
        "1leading-digit",
        "-leading-dash",
        "Acme",         # uppercase
        "acme_corp",    # underscore
        "acme corp",    # space
        "acme.corp",    # dot
        "acme/corp",    # slash (path traversal vector)
        "acme\\corp",   # backslash
        "acme\ncorp",   # newline (multi-line bypass guard)
        "acmeé",   # non-ASCII
        # 64 chars: one over the cap.
        "a234567890123456789012345678901234567890123456789012345678901bcd",
    ],
)
def test_rejects_invalid_slugs(bad: str) -> None:
    with pytest.raises(TenantIdError, match="must match"):
        TenantId.new(bad)


def test_root_dir_under_tenants_subdir() -> None:
    root = Path("/data")
    tenant = TenantId.new("acme")
    assert tenant_root_dir(root, tenant) == Path("/data/tenants/acme")


def test_db_path_appends_sqlite_suffix_once() -> None:
    root = Path("/data")
    tenant = TenantId.new("acme")
    assert tenant_db_path(root, tenant, "sessions") == Path(
        "/data/tenants/acme/sessions.sqlite"
    )
    assert tenant_db_path(root, tenant, "evolution") == Path(
        "/data/tenants/acme/evolution.sqlite"
    )


def test_legacy_default_layout() -> None:
    root = Path("/data")
    tenant = TenantId.legacy_default()
    assert tenant_db_path(root, tenant, "sessions") == Path(
        "/data/tenants/default/sessions.sqlite"
    )


def test_tenant_id_is_hashable() -> None:
    # Frozen dataclass should drop into a dict / set without surprises.
    s = {TenantId.new("acme"), TenantId.new("acme"), TenantId.new("bravo")}
    assert len(s) == 2
