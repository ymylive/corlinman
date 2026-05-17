"""``/admin/profiles*`` — CRUD over the profile registry.

Wave 3.1 of ``docs/PLAN_EASY_SETUP.md``.

A *profile* is an isolated agent instance with its own persona, memory,
skills, and state. The store + on-disk layout live in
:mod:`corlinman_server.profiles`; this route module is a thin FastAPI
adapter that maps :class:`ProfileError` subclasses to HTTP status codes.

Seven routes:

* ``GET    /admin/profiles``                — list every profile
* ``POST   /admin/profiles``                — create (optionally clone)
* ``GET    /admin/profiles/{slug}``         — fetch one
* ``PATCH  /admin/profiles/{slug}``         — partial update
* ``DELETE /admin/profiles/{slug}``         — remove
* ``GET    /admin/profiles/{slug}/soul``    — read SOUL.md
* ``PUT    /admin/profiles/{slug}/soul``    — atomic-write SOUL.md (W3.2)

All mount behind :func:`require_admin_dependency` — same pattern
as ``/admin/agents*`` etc.

State plumbing: the bootstrapper hands a :class:`ProfileStore` instance
to :class:`AdminState.profile_store`. Handlers 503 with
``profile_store_missing`` when the field is ``None`` so unit tests
that build a stripped-down state without the store don't accidentally
exercise the disk path.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.profiles import (
    Profile,
    ProfileExists,
    ProfileNotFound,
    ProfileProtected,
    ProfileSlugInvalid,
    ProfileStore,
    profile_soul_path,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class ProfileOut(BaseModel):
    """Wire shape for a profile row. ``created_at`` is ISO-8601 / RFC-3339
    UTC with a ``Z`` suffix — matches the rest of the admin surface."""

    slug: str
    display_name: str
    created_at: str
    parent_slug: str | None = None
    description: str | None = None


class CreateProfileRequest(BaseModel):
    """``POST /admin/profiles`` body.

    ``slug`` is required. ``display_name`` defaults to ``slug`` server-side
    when omitted. ``clone_from`` is the parent slug to copy SOUL/MEMORY/
    USER/skills from — must already exist.
    """

    slug: str = Field(..., description="Profile slug — see SLUG_REGEX")
    display_name: str | None = Field(
        default=None, description="Human-readable name; defaults to slug"
    )
    clone_from: str | None = Field(
        default=None, description="Optional parent slug to copy from"
    )
    description: str | None = Field(
        default=None, description="Optional free-text blurb"
    )


class UpdateProfileRequest(BaseModel):
    """``PATCH /admin/profiles/{slug}`` body — both fields optional."""

    display_name: str | None = None
    description: str | None = None


class SoulOut(BaseModel):
    """``GET /admin/profiles/{slug}/soul`` response.

    ``content`` is the raw markdown body of ``SOUL.md``; empty string when
    the file is missing (we treat absence as "blank persona" rather than
    404 — the placeholder gets created on first profile materialisation
    but a stale install might lack it). The route enforces that the
    *profile* exists upstream.
    """

    content: str


class SoulIn(BaseModel):
    """``PUT /admin/profiles/{slug}/soul`` body."""

    content: str = Field(..., description="Full SOUL.md body (markdown)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: _dt.datetime) -> str:
    """Render a tz-aware datetime as RFC-3339 with a ``Z`` suffix.

    Duplicated from :mod:`corlinman_server.profiles.store` to avoid the
    route module taking a dep on a private helper. The format must stay
    in sync.
    """
    return dt.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _to_out(profile: Profile) -> ProfileOut:
    return ProfileOut(
        slug=profile.slug,
        display_name=profile.display_name,
        created_at=_iso(profile.created_at),
        parent_slug=profile.parent_slug,
        description=profile.description,
    )


def _profile_store(state: AdminState) -> ProfileStore:
    """Return the wired :class:`ProfileStore` or raise 503.

    Mirrors the ``_ensure_session_store`` pattern in ``auth.py`` —
    handlers that need the store call this first so the failure mode
    is a single readable envelope rather than a 500 from a ``None``
    attribute access.
    """
    store = getattr(state, "profile_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "profile_store_missing"},
        )
    return store


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/profiles*``. Mounted by
    :func:`corlinman_server.gateway.routes_admin_a.build_router`."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/profiles",
        response_model=list[ProfileOut],
        summary="List every profile",
    )
    async def list_profiles(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[ProfileOut]:
        store = _profile_store(state)
        return [_to_out(p) for p in store.list()]

    @r.post(
        "/admin/profiles",
        response_model=ProfileOut,
        status_code=status.HTTP_201_CREATED,
        summary="Create one profile (optionally cloning a parent)",
    )
    async def create_profile(
        body: CreateProfileRequest,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ProfileOut:
        store = _profile_store(state)
        try:
            profile = store.create(
                slug=body.slug,
                display_name=body.display_name,
                parent_slug=body.clone_from,
                description=body.description,
            )
        except ProfileSlugInvalid as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_slug",
                    "message": str(exc),
                },
            ) from exc
        except ProfileExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "profile_exists",
                    "slug": body.slug,
                    "message": str(exc),
                },
            ) from exc
        except ProfileNotFound as exc:
            # Only :meth:`ProfileStore.create` raises this for missing
            # ``parent_slug`` (the slug under create is the *new* one, so
            # ProfileNotFound here is unambiguously about the parent).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "parent_not_found",
                    "slug": body.clone_from,
                    "message": str(exc),
                },
            ) from exc
        return _to_out(profile)

    @r.get(
        "/admin/profiles/{slug}",
        response_model=ProfileOut,
        summary="Fetch one profile",
    )
    async def get_profile(
        slug: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ProfileOut:
        store = _profile_store(state)
        profile = store.get(slug)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "profile_not_found",
                    "slug": slug,
                },
            )
        return _to_out(profile)

    @r.patch(
        "/admin/profiles/{slug}",
        response_model=ProfileOut,
        summary="Partial update of a profile's metadata",
    )
    async def update_profile(
        slug: str,
        body: UpdateProfileRequest,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ProfileOut:
        store = _profile_store(state)
        try:
            profile = store.update(
                slug,
                display_name=body.display_name,
                description=body.description,
            )
        except ProfileNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "profile_not_found",
                    "slug": slug,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_payload",
                    "message": str(exc),
                },
            ) from exc
        return _to_out(profile)

    @r.delete(
        "/admin/profiles/{slug}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Delete one profile",
    )
    async def delete_profile(
        slug: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        store = _profile_store(state)
        try:
            removed = store.delete(slug)
        except ProfileProtected as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "profile_protected",
                    "slug": slug,
                    "message": str(exc),
                },
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "profile_not_found",
                    "slug": slug,
                },
            )
        # 204 — empty body. ``response_class=Response`` keeps FastAPI from
        # trying to serialise a response_model into the 204 envelope.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- SOUL.md editor surface (W3.2) ----------------------------------
    #
    # Two narrow endpoints so the UI can lazy-load the persona markdown
    # for the profile row's expand-to-edit affordance and atomic-write
    # it back. Both require the profile to exist (404 ``profile_not_found``
    # mirrors the GET/PATCH/DELETE convention above).
    #
    # ``GET`` returns ``{content: ""}`` when the file is missing — the
    # placeholder file is created on profile materialisation but a stale
    # install or a hand-edited filesystem might lack it; "blank persona"
    # is a more useful failure mode than a 404 the editor has to special-
    # case.
    #
    # ``PUT`` does an atomic write via tempfile + ``os.replace`` so a
    # crash mid-write leaves the previous SOUL intact (the persona is
    # read on every chat turn — partial files would corrupt the agent
    # mid-conversation).

    @r.get(
        "/admin/profiles/{slug}/soul",
        response_model=SoulOut,
        summary="Read the profile's SOUL.md content",
    )
    async def get_profile_soul(
        slug: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SoulOut:
        store = _profile_store(state)
        if store.get(slug) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "profile_not_found", "slug": slug},
            )
        soul_path = profile_soul_path(store.data_dir, slug)
        try:
            content = soul_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Treat absence as "blank persona" — see module docstring.
            content = ""
        return SoulOut(content=content)

    @r.put(
        "/admin/profiles/{slug}/soul",
        response_model=SoulOut,
        summary="Atomic-write the profile's SOUL.md",
    )
    async def put_profile_soul(
        slug: str,
        body: SoulIn,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SoulOut:
        store = _profile_store(state)
        if store.get(slug) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "profile_not_found", "slug": slug},
            )
        soul_path = profile_soul_path(store.data_dir, slug)
        # Ensure parent dir exists — defensive; the create path makes it
        # but a hand-rolled profile dir might not.
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tempfile in the same dir (same filesystem,
        # rename is atomic) → ``os.replace`` swap.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".soul-", suffix=".tmp", dir=str(soul_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(body.content)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_name, soul_path)
        except Exception:
            # Best-effort cleanup of the staging file on failure.
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        return SoulOut(content=body.content)

    return r


__all__ = [
    "CreateProfileRequest",
    "ProfileOut",
    "SoulIn",
    "SoulOut",
    "UpdateProfileRequest",
    "router",
]
