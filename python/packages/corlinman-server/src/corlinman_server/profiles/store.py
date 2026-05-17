"""SQLite-backed profile registry.

A *profile* is an isolated agent instance with its own persona, memory,
skills, and state. This module owns the index table; the on-disk
directory tree under ``<data_dir>/profiles/<slug>/`` is materialised by
:func:`corlinman_server.profiles.paths.ensure_profile_dirs`.

Design choices
--------------

* **One row per profile.** ``slug`` is the primary key — it doubles as
  the directory name and is the user-facing identifier in URLs.
* **Soft schema migration in __init__.** We're on schema v1, so the
  store applies a single idempotent ``CREATE TABLE IF NOT EXISTS``.
  Future versions can layer ``ALTER TABLE`` calls behind a ``PRAGMA
  user_version`` check (same convention the tenancy / evolution stores
  use elsewhere in the codebase).
* **Sync sqlite3, not aiosqlite.** Profile mutations are infrequent
  (operator UI clicks) and tiny (a row + maybe a directory copy). The
  sync API keeps the API itself sync, which is what the FastAPI routes
  in ``routes_admin_a/profiles.py`` want — they call into the store
  directly from ``async def`` handlers but the underlying work is tiny.
  This matches the pattern in
  :mod:`corlinman_server.tenancy.admin_schema` which is also synchronous
  even though sibling stores use aiosqlite.
* **WAL + foreign_keys ON.** Mirrors the rest of the corlinman SQLite
  stores.
* **No external locking.** SQLite serialises writes internally; the
  store holds a single connection per instance and operations are
  short. If callers need to coordinate across processes they should
  layer a flock on top.

Reserved slugs
--------------

The slug ``"default"`` is the bootstrap profile created on first boot.
It cannot be deleted (raises :class:`ProfileProtected`). All other
slug validation lives in :func:`corlinman_server.profiles.paths.validate_slug`.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import structlog

from corlinman_server.profiles.paths import (
    ensure_profile_dirs,
    profile_root,
    validate_slug,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SLUG: str = "default"
"""Reserved bootstrap profile slug — cannot be deleted."""

INDEX_DB_NAME: str = "index.sqlite"
"""SQLite file name under ``<data_dir>/profiles/`` holding the registry."""

# Files copied from parent → child on clone. Keep this list small and
# explicit — copying the entire profile_root would also copy state.db
# (per-profile session state) which would conflate two distinct
# personas' chat histories.
_CLONE_FILES: tuple[str, ...] = ("SOUL.md", "MEMORY.md", "USER.md")
"""Top-level files copied on ``clone_from`` (skills/ is copied recursively)."""

_SCHEMA_SQL: str = r"""
CREATE TABLE IF NOT EXISTS profiles (
    slug          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    parent_slug   TEXT,
    description   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_created_at
    ON profiles(created_at);
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProfileError(Exception):
    """Base class for profile-store domain errors.

    Route layer maps these to HTTP status codes; CLI / tests pattern-match
    on the subclass.
    """


class ProfileExists(ProfileError):
    """Raised when :meth:`ProfileStore.create` is called with a slug that
    is already registered. Maps to HTTP 409."""


class ProfileNotFound(ProfileError):
    """Raised when an operation references a non-existent slug. Maps to
    HTTP 404."""


class ProfileSlugInvalid(ProfileError):
    """Raised when a slug fails :func:`validate_slug`. Maps to HTTP 422."""


class ProfileProtected(ProfileError):
    """Raised when an operation would mutate the reserved ``default``
    profile in a forbidden way (e.g., delete). Maps to HTTP 409."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """One row from the ``profiles`` table.

    Frozen so callers can stash returns without worrying about aliasing.
    ``created_at`` is timezone-aware UTC; the SQLite column stores an
    ISO-8601 string with a ``Z`` suffix (mirroring the rest of the
    corlinman codebase).
    """

    slug: str
    display_name: str
    created_at: _dt.datetime
    parent_slug: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _utc_now() -> _dt.datetime:
    """Wall-clock UTC. Pulled out so tests can freeze time via monkeypatch
    of ``corlinman_server.profiles.store._utc_now``."""
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    """Render a tz-aware datetime as RFC-3339 / ISO-8601 with ``Z`` suffix.

    Matches the format used by :mod:`corlinman_server.gateway.routes_admin_a.auth`
    so the wire vocabulary is consistent across admin routes.
    """
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> _dt.datetime:
    """Parse the ISO-8601 ``Z`` string written by :func:`_iso` back into
    a tz-aware datetime. Accepts the legacy ``+00:00`` suffix too."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(value)


class ProfileStore:
    """CRUD wrapper over the ``profiles`` table.

    Construct with the directory that should hold ``profiles/`` — both
    the index DB and the per-profile subdirectories live under it. The
    constructor opens the SQLite connection eagerly and applies the
    schema; tests can pass any writable path.

    Thread safety: holds a single :class:`sqlite3.Connection` with
    ``check_same_thread=False``. SQLite serialises writes via its own
    file lock; we add a Python-level ``threading.Lock`` so concurrent
    callers on the same connection don't trip
    ``sqlite3.OperationalError: database is locked`` on the read side.
    """

    def __init__(self, profiles_dir: Path) -> None:
        """Open the index DB at ``<profiles_dir>/index.sqlite``.

        ``profiles_dir`` is the *parent* of the per-profile subdirectories
        — the entrypoint passes ``data_dir / "profiles"``. The directory
        is created on demand.
        """
        self._profiles_dir = Path(profiles_dir)
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._profiles_dir / INDEX_DB_NAME
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we batch via explicit BEGIN
        )
        # WAL + foreign-keys mirrors the rest of corlinman's sqlite stores.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)

    # ---- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover — best-effort drain
                pass

    @property
    def profiles_dir(self) -> Path:
        """Parent directory that holds per-profile subdirs + the index DB."""
        return self._profiles_dir

    @property
    def data_dir(self) -> Path:
        """Synthetic ``data_dir`` such that
        ``profile_root(data_dir, slug) == profiles_dir / slug``.

        Computed as ``profiles_dir.parent`` so callers that want to use
        :mod:`corlinman_server.profiles.paths` helpers can do so without
        threading two paths through every call site.
        """
        return self._profiles_dir.parent

    # ---- internal helpers ---------------------------------------------------

    def _row_to_profile(self, row: tuple) -> Profile:
        slug, display_name, parent_slug, description, created_at, _updated = row
        return Profile(
            slug=str(slug),
            display_name=str(display_name),
            created_at=_parse_iso(str(created_at)),
            parent_slug=(str(parent_slug) if parent_slug is not None else None),
            description=(str(description) if description is not None else None),
        )

    def _get_row(self, slug: str) -> tuple | None:
        cursor = self._conn.execute(
            "SELECT slug, display_name, parent_slug, description, created_at, updated_at "
            "FROM profiles WHERE slug = ?",
            (slug,),
        )
        return cursor.fetchone()

    # ---- CRUD ---------------------------------------------------------------

    def exists(self, slug: str) -> bool:
        """``True`` if ``slug`` is registered. Does not raise on invalid
        slugs — callers that care should call :func:`validate_slug` first."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM profiles WHERE slug = ? LIMIT 1",
                (slug,),
            )
            return cursor.fetchone() is not None

    def get(self, slug: str) -> Profile | None:
        """Fetch one profile by slug, or ``None`` when missing."""
        with self._lock:
            row = self._get_row(slug)
        return self._row_to_profile(row) if row is not None else None

    def list(self) -> list[Profile]:
        """All profiles, ordered by ``created_at ASC`` so the bootstrap
        ``default`` profile lands first in the UI listing."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT slug, display_name, parent_slug, description, created_at, updated_at "
                "FROM profiles "
                "ORDER BY created_at ASC, slug ASC"
            )
            rows = cursor.fetchall()
        return [self._row_to_profile(r) for r in rows]

    def create(
        self,
        slug: str,
        display_name: str | None = None,
        parent_slug: str | None = None,
        description: str | None = None,
    ) -> Profile:
        """Insert one profile and materialise its directory tree.

        * ``display_name`` defaults to ``slug`` so the wire shape always
          has a user-facing label (the UI's ``displayName ?? slug``
          fallback is handled here for consistency).
        * When ``parent_slug`` is set, the parent's ``SOUL.md`` / ``MEMORY.md``
          / ``USER.md`` and the entire ``skills/`` subtree are copied
          into the new profile (mirrors hermes's ``clone_from_default``
          flow in ``ProfilesPage.tsx``). The parent's ``state.db`` is
          **not** copied — conversation state stays per-profile.
        * Raises :class:`ProfileSlugInvalid` when ``slug`` fails the regex.
        * Raises :class:`ProfileExists` when ``slug`` is already taken.
        * Raises :class:`ProfileNotFound` when ``parent_slug`` doesn't
          exist (only checked when ``parent_slug`` is not ``None``).
        """
        try:
            validate_slug(slug)
        except ValueError as exc:
            raise ProfileSlugInvalid(str(exc)) from exc

        if parent_slug is not None:
            try:
                validate_slug(parent_slug)
            except ValueError as exc:
                raise ProfileSlugInvalid(
                    f"parent_slug: {exc}"
                ) from exc

        now = _utc_now()
        now_iso = _iso(now)
        effective_display = display_name if display_name else slug

        with self._lock:
            if self._get_row(slug) is not None:
                raise ProfileExists(f"profile {slug!r} already exists")
            if parent_slug is not None and self._get_row(parent_slug) is None:
                raise ProfileNotFound(
                    f"parent profile {parent_slug!r} does not exist"
                )

            # Materialise the directory tree *before* the DB row so a
            # crash during disk setup doesn't leave a row pointing at
            # nothing. If the DB insert fails after this we leave the
            # directory on disk — a noop retry from the operator will
            # idempotently see it and proceed.
            ensure_profile_dirs(self.data_dir, slug)

            if parent_slug is not None:
                self._clone_files(parent_slug, slug)

            try:
                self._conn.execute(
                    "INSERT INTO profiles "
                    "(slug, display_name, parent_slug, description, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        slug,
                        effective_display,
                        parent_slug,
                        description,
                        now_iso,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # PRIMARY KEY collision raced past the existence check
                # — treat the same as the upfront ProfileExists branch
                # so callers see a single failure mode.
                raise ProfileExists(
                    f"profile {slug!r} already exists"
                ) from exc

        logger.info(
            "profiles.created",
            slug=slug,
            display_name=effective_display,
            parent_slug=parent_slug,
        )
        return Profile(
            slug=slug,
            display_name=effective_display,
            created_at=now,
            parent_slug=parent_slug,
            description=description,
        )

    def rename(self, slug: str, new_display_name: str) -> Profile:
        """Update ``display_name`` (and bump ``updated_at``). Slug is
        immutable — to rename the slug itself, delete + recreate.

        Raises :class:`ProfileNotFound` when the slug doesn't exist. The
        name "rename" is intentional (it mirrors the hermes UI button
        label) even though only the display name changes.
        """
        if not isinstance(new_display_name, str) or not new_display_name.strip():
            raise ValueError("new_display_name must be a non-empty string")
        now_iso = _iso(_utc_now())
        with self._lock:
            row = self._get_row(slug)
            if row is None:
                raise ProfileNotFound(f"profile {slug!r} does not exist")
            self._conn.execute(
                "UPDATE profiles SET display_name = ?, updated_at = ? WHERE slug = ?",
                (new_display_name, now_iso, slug),
            )
            row = self._get_row(slug)
        assert row is not None  # we just updated it
        return self._row_to_profile(row)

    def update(
        self,
        slug: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> Profile:
        """Partial update — same semantics as :meth:`rename` but also
        allows mutating ``description`` independently. Passing ``None``
        for a field leaves it unchanged.

        Used by the ``PATCH /admin/profiles/{slug}`` route. Returns the
        post-update row.
        """
        if display_name is None and description is None:
            # No-op patch: return the current row without touching the
            # ``updated_at`` stamp so idempotent re-applies stay quiet.
            profile = self.get(slug)
            if profile is None:
                raise ProfileNotFound(f"profile {slug!r} does not exist")
            return profile

        sets: list[str] = []
        params: list[object] = []
        if display_name is not None:
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("display_name must be a non-empty string")
            sets.append("display_name = ?")
            params.append(display_name)
        if description is not None:
            # Empty string is allowed — interpret it as "clear the field".
            sets.append("description = ?")
            params.append(description if description != "" else None)
        sets.append("updated_at = ?")
        params.append(_iso(_utc_now()))
        params.append(slug)

        with self._lock:
            row = self._get_row(slug)
            if row is None:
                raise ProfileNotFound(f"profile {slug!r} does not exist")
            self._conn.execute(
                f"UPDATE profiles SET {', '.join(sets)} WHERE slug = ?",
                params,
            )
            row = self._get_row(slug)
        assert row is not None
        return self._row_to_profile(row)

    def delete(self, slug: str) -> bool:
        """Remove the row + recursively rm the profile directory.

        Returns ``True`` on success, ``False`` when the slug didn't
        exist (idempotent). Raises :class:`ProfileProtected` when
        called against the reserved ``default`` slug — operators that
        really want to remove default must wipe the data dir manually.
        """
        if slug == DEFAULT_SLUG:
            raise ProfileProtected(
                f"profile {DEFAULT_SLUG!r} is the bootstrap profile and "
                "cannot be deleted"
            )
        with self._lock:
            row = self._get_row(slug)
            if row is None:
                return False
            self._conn.execute(
                "DELETE FROM profiles WHERE slug = ?",
                (slug,),
            )
        root = profile_root(self.data_dir, slug)
        if root.exists():
            try:
                shutil.rmtree(root)
            except OSError as exc:
                # The row is gone but the directory isn't — log so the
                # operator can clean up by hand, but don't fail the call
                # (the index is authoritative for "does this profile
                # exist?" queries).
                logger.warning(
                    "profiles.delete.rmtree_failed",
                    slug=slug,
                    path=str(root),
                    error=str(exc),
                )
        logger.info("profiles.deleted", slug=slug)
        return True

    # ---- clone helper -------------------------------------------------------

    def _clone_files(self, parent_slug: str, child_slug: str) -> None:
        """Copy persona + memory + skills/ from parent → child.

        Called from :meth:`create` after the child's directory tree has
        been materialised. State DBs are intentionally **not** copied —
        chat history stays per-profile.
        """
        parent_root = profile_root(self.data_dir, parent_slug)
        child_root = profile_root(self.data_dir, child_slug)
        for name in _CLONE_FILES:
            src = parent_root / name
            dst = child_root / name
            if not src.exists():
                continue
            try:
                dst.write_bytes(src.read_bytes())
            except OSError as exc:
                logger.warning(
                    "profiles.clone.file_copy_failed",
                    parent=parent_slug,
                    child=child_slug,
                    file=name,
                    error=str(exc),
                )
        # skills/ — recursive copy. We use copytree(dirs_exist_ok=True)
        # so an empty pre-created child skills/ dir doesn't blow up.
        src_skills = parent_root / "skills"
        dst_skills = child_root / "skills"
        if src_skills.is_dir():
            try:
                shutil.copytree(src_skills, dst_skills, dirs_exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "profiles.clone.skills_copy_failed",
                    parent=parent_slug,
                    child=child_slug,
                    error=str(exc),
                )


__all__ = [
    "DEFAULT_SLUG",
    "Profile",
    "ProfileError",
    "ProfileExists",
    "ProfileNotFound",
    "ProfileProtected",
    "ProfileSlugInvalid",
    "ProfileStore",
]
