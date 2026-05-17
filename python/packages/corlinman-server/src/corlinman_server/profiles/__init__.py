"""Profile registry + on-disk layout.

A *profile* is an isolated agent instance with its own persona, memory,
skills, and state. Borrowed from hermes-agent's
``~/.hermes/profiles/<slug>/`` pattern; see
:mod:`corlinman_server.profiles.paths` for the on-disk layout and
:mod:`corlinman_server.profiles.store` for the SQLite index.

Two surfaces:

* :class:`ProfileStore` — SQLite-backed CRUD over the ``profiles`` table
  + filesystem materialisation. Used by the
  ``/admin/profiles*`` routes in
  :mod:`corlinman_server.gateway.routes_admin_a.profiles`.
* :func:`ensure_profile_dirs` and friends — pure path helpers, importable
  without spinning up SQLite.

Wave 3.1 of ``docs/PLAN_EASY_SETUP.md``.
"""

from __future__ import annotations

from corlinman_server.profiles.paths import (
    SLUG_REGEX,
    ensure_profile_dirs,
    profile_memory_path,
    profile_root,
    profile_skills_dir,
    profile_soul_path,
    profile_state_db,
    profile_user_path,
    validate_slug,
)
from corlinman_server.profiles.store import (
    DEFAULT_SLUG,
    Profile,
    ProfileError,
    ProfileExists,
    ProfileNotFound,
    ProfileProtected,
    ProfileSlugInvalid,
    ProfileStore,
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
    "SLUG_REGEX",
    "ensure_profile_dirs",
    "profile_memory_path",
    "profile_root",
    "profile_skills_dir",
    "profile_soul_path",
    "profile_state_db",
    "profile_user_path",
    "validate_slug",
]
