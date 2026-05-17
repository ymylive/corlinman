"""``TenantId`` — slug-shaped newtype (Python port of ``corlinman-tenant::id``).

Phase 3.1 (Tier 3 / S-2) seeded ``user_traits.tenant_id`` and
``agent_persona_state.tenant_id`` as ``TEXT NOT NULL DEFAULT 'default'``,
so the de-facto wire shape is "slug or the literal 'default'". The
Python wrapper enforces that shape at the language boundary so a
mistyped string in admin-claim parsing or a ``?tenant=`` query param
can't smuggle SQL fragments / path traversal segments / non-ASCII
into the per-tenant directory layout.

Shape: ``^[a-z][a-z0-9-]{0,62}$``
  * starts with a lowercase letter (no leading digit / dash so
    filenames sort intuitively and don't collide with reserved
    conventions)
  * lowercase alphanumeric + ASCII hyphen only — no underscore (looks
    like a typo of dash in URLs), no uppercase (case-folding bugs on
    filesystems vary by host)
  * 1-63 chars total — same upper bound as DNS labels, so a tenant id
    drops cleanly into a hostname / cookie segment / S3 prefix
    without a separate length cap

``default`` is the reserved value for legacy single-tenant boots. It
still passes the slug regex (it's lowercase letters), but
:meth:`TenantId.legacy_default` returns it explicitly so call-sites that
"just want the legacy tenant" don't have to spell it.
"""

from __future__ import annotations

import re
from functools import total_ordering
from typing import Any

# Reserved tenant id for legacy / single-tenant deployments.
#
# Every Phase 3.1 SQLite row was stamped with this value via the
# column default; the constant is exported here so the rest of the
# codebase doesn't have to spell the literal string at call sites
# (typos in a literal slip through compilation).
DEFAULT_TENANT_ID: str = "default"

# Canonical slug regex string. Source of truth for the cross-language
# tenant slug contract documented at ``docs/contracts/tenant-slug.md``.
# The TypeScript validator in ``ui/lib/api/tenants.ts`` and the Rust
# ``corlinman-tenant`` crate MUST mirror this pattern byte-for-byte.
#
# Note: Rust uses ``\A...\z`` (which in Python regex translates to
# ``\A...\Z``) to forbid trailing newline matches that ``$`` would
# permit. Python's :func:`re.fullmatch` already requires the *whole*
# string to match, but a literal ``\n`` inside the candidate would
# still be rejected by the negated character class — both layers of
# defence are kept.
TENANT_SLUG_REGEX_STR: str = r"\A[a-z][a-z0-9-]{0,62}\Z"

# Compiled once at import time. The regex is module-level so we don't
# pay the compile cost on every ``TenantId.new`` (validation runs in
# admin auth hot paths).
_TENANT_ID_RE = re.compile(TENANT_SLUG_REGEX_STR)


class TenantIdError(ValueError):
    """Base class for tenant-id validation failures.

    Subclassed by :class:`TenantIdEmpty` and :class:`TenantIdInvalidShape`
    so callers (e.g. a gateway middleware mapping errors → HTTP 400)
    can distinguish the two without parsing strings.
    """


class TenantIdEmpty(TenantIdError):  # noqa: N818 — mirrors Rust `TenantIdError::Empty`
    """Input was the empty string.

    Operator typing ``?tenant=`` with an empty value is the most common
    mistake — we keep it as its own subclass so the gateway middleware
    can return a more helpful 400 message.
    """

    def __init__(self) -> None:
        super().__init__("tenant id must not be empty")


class TenantIdInvalidShape(TenantIdError):  # noqa: N818 — mirrors Rust `TenantIdError::InvalidShape`
    """Input is non-empty but didn't match the slug regex.

    The offending value is included so logs let the operator copy the
    string back into a config.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(
            f"tenant id {value!r} must match ^[a-z][a-z0-9-]{{0,62}}$ "
            "(lowercase ASCII alphanumeric + hyphen, 1-63 chars, must "
            "start with a letter)"
        )


@total_ordering
class TenantId:
    """Tenant identifier. Cheap to construct (single ``str`` reference).

    Constructed only via :meth:`new` / :meth:`from_str` / pydantic-style
    validators — every code path runs the same slug check. Instances are
    immutable (the wrapped value is read-only) and hashable, so they
    work as dict keys.
    """

    __slots__ = ("_value",)

    # Slot type hint for mypy — the actual assignment happens via
    # `object.__setattr__` (the instance is otherwise immutable, which
    # mypy doesn't model natively for `__slots__`).
    _value: str

    def __init__(self, value: str) -> None:
        # Direct calls must still pass validation. Internal fast-path
        # constructors (e.g. :meth:`legacy_default`) bypass the regex
        # via :meth:`_unchecked` after asserting at module scope.
        if not isinstance(value, str):
            raise TenantIdInvalidShape(str(value))
        if value == "":
            raise TenantIdEmpty()
        if _TENANT_ID_RE.fullmatch(value) is None:
            raise TenantIdInvalidShape(value)
        object.__setattr__(self, "_value", value)

    # ---- alternate constructors ------------------------------------------------

    @classmethod
    def new(cls, value: str) -> TenantId:
        """Validate ``value`` and wrap it in a :class:`TenantId`.

        Mirrors ``TenantId::new`` in the Rust crate. Raises
        :class:`TenantIdEmpty` or :class:`TenantIdInvalidShape` on
        rejection.
        """
        return cls(value)

    @classmethod
    def from_str(cls, value: str) -> TenantId:
        """Alias for :meth:`new` (mirrors Rust's ``FromStr`` impl)."""
        return cls(value)

    @classmethod
    def legacy_default(cls) -> TenantId:
        """The reserved legacy tenant id.

        This is the value Phase 3.1's ``'default'`` column stamp used
        and the value returned by zero-arg call-sites that "just want
        the legacy tenant". Wrapping it in a class method keeps the
        literal in one place.
        """
        return cls._unchecked(DEFAULT_TENANT_ID)

    @classmethod
    def _unchecked(cls, value: str) -> TenantId:
        """Skip regex validation. Internal — only for values known
        statically to satisfy the slug shape."""
        inst = object.__new__(cls)
        object.__setattr__(inst, "_value", value)
        return inst

    # ---- accessors -------------------------------------------------------------

    def as_str(self) -> str:
        """Borrow the underlying slug string (mirrors Rust ``as_str``)."""
        return self._value

    def into_inner(self) -> str:
        """Take the inner :class:`str`. Used by serialisation / ETL
        paths that want to hand the raw value to a SQLite bind without
        an extra allocation."""
        return self._value

    def is_legacy_default(self) -> bool:
        """True iff this is the reserved legacy value. Callers that
        want to keep "single-tenant compat" branches readable should
        prefer this over ``==`` on the underlying string."""
        return self._value == DEFAULT_TENANT_ID

    # ---- dunder protocols ------------------------------------------------------

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"TenantId({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TenantId):
            return self._value == other._value
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, TenantId):
            return self._value < other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    # ---- serde-compatible hooks ------------------------------------------------

    def to_json(self) -> str:
        """Serialise as a bare slug string. Mirrors the Rust
        ``Serialize`` impl which emits the value, not a wrapper object."""
        return self._value

    @classmethod
    def from_json(cls, value: Any) -> TenantId:
        """Re-validate a value coming from JSON / TOML deserialisation.

        Integration-level guard: a malicious config file must not
        smuggle a path-traversal segment through the deserialiser.
        """
        if not isinstance(value, str):
            raise TenantIdInvalidShape(str(value))
        return cls.new(value)


def default_tenant() -> TenantId:
    """Free function spelling of :meth:`TenantId.legacy_default`.

    Provided for parity with Rust's ``TenantId::default()``; lets call
    sites use ``default_tenant()`` instead of the class-method form
    when that reads more naturally.
    """
    return TenantId.legacy_default()


__all__ = [
    "DEFAULT_TENANT_ID",
    "TENANT_SLUG_REGEX_STR",
    "TenantId",
    "TenantIdEmpty",
    "TenantIdError",
    "TenantIdInvalidShape",
    "default_tenant",
]
