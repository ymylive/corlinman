"""Local tenant slug type + filesystem-path helpers.

The Rust crate ``corlinman-replay`` depends on ``corlinman_tenant::TenantId``
and ``corlinman_tenant::tenant_db_path``. The Python sibling keeps the same
shape but copies the helpers locally so this package does not couple to
any specific sibling (e.g. ``corlinman-evolution-store``) — the task
brief explicitly calls out using a local Protocol / NewType to avoid
that coupling.

Shape contract (mirrors ``rust/crates/corlinman-tenant/src/id.rs``):
``^[a-z][a-z0-9-]{0,62}$`` — lowercase ASCII alphanumeric + hyphen,
1-63 chars, must start with a letter. ``default`` is the reserved
legacy slug for single-tenant boots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Reserved tenant id for legacy / single-tenant deployments. Every
# pre-Phase-4 SQLite row was stamped with this via the column default.
DEFAULT_TENANT_ID: str = "default"

# Canonical slug regex. Source of truth is
# ``rust/crates/corlinman-tenant/src/id.rs`` -- the Rust version uses
# ``\A`` / ``\z`` so multi-line strings cannot sneak through with a
# slug on the first line. Python's ``re`` doesn't support ``\z`` (it
# spells the no-newline end-anchor ``\Z``), so we expose the Rust
# spelling as :data:`TENANT_SLUG_REGEX_STR` for cross-language docs
# and compile the Python-flavoured equivalent locally.
TENANT_SLUG_REGEX_STR: str = r"\A[a-z][a-z0-9-]{0,62}\z"
_TENANT_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,62}\Z")


class TenantIdError(ValueError):
    """Validation failure for a candidate tenant id.

    The two failure modes from the Rust enum collapse into one Python
    exception. The message text distinguishes empty vs invalid-shape so
    callers can pattern-match the prefix if they need to.
    """


@dataclass(frozen=True, slots=True)
class TenantId:
    """Tenant identifier. Frozen so it hashes (drop into ``dict`` /
    ``set`` keys without surprises).

    Construct via :meth:`TenantId.new` or :meth:`TenantId.from_str`
    every code path runs the same slug check. :meth:`legacy_default`
    returns the reserved ``default`` slug without re-running the regex
    on a known-good literal.
    """

    _value: str

    @classmethod
    def new(cls, value: str) -> TenantId:
        """Validate ``value`` and wrap it in a :class:`TenantId`."""
        if value == "":
            raise TenantIdError("tenant id must not be empty")
        if not _TENANT_ID_RE.match(value):
            raise TenantIdError(
                f"tenant id {value!r} must match ^[a-z][a-z0-9-]{{0,62}}$ "
                "(lowercase ASCII alphanumeric + hyphen, 1-63 chars, must "
                "start with a letter)"
            )
        return cls(value)

    @classmethod
    def from_str(cls, value: str) -> TenantId:
        """Alias of :meth:`new` matching the Rust ``FromStr`` impl."""
        return cls.new(value)

    @classmethod
    def legacy_default(cls) -> TenantId:
        """The reserved legacy tenant id (``"default"``). Skips the regex
        check on a compile-time-known good literal."""
        return cls(DEFAULT_TENANT_ID)

    def as_str(self) -> str:
        """Borrow the underlying slug string."""
        return self._value

    def is_legacy_default(self) -> bool:
        """True iff this is the reserved ``"default"`` value."""
        return self._value == DEFAULT_TENANT_ID

    def __str__(self) -> str:
        return self._value


def tenant_root_dir(root: Path, tenant: TenantId) -> Path:
    """Path to the directory holding all per-tenant data files. Layout:

    ``<root>/tenants/<tenant_id>/``
    """
    return root / "tenants" / tenant.as_str()


def tenant_db_path(root: Path, tenant: TenantId, name: str) -> Path:
    """Full path for the per-tenant SQLite file named ``name``.

    Example: ``tenant_db_path(root, acme, "sessions")`` ->
    ``<root>/tenants/acme/sessions.sqlite``.

    ``name`` is taken bare (no ``.sqlite`` suffix) so call-sites read
    like the legacy single-tenant constants and a stray ``name =
    "agent_state.bak"`` cannot produce a double-suffix path.
    """
    return tenant_root_dir(root, tenant) / f"{name}.sqlite"


__all__ = [
    "DEFAULT_TENANT_ID",
    "TENANT_SLUG_REGEX_STR",
    "TenantId",
    "TenantIdError",
    "tenant_db_path",
    "tenant_root_dir",
]
