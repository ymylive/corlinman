"""``/admin/identity*`` — operator-facing identity surface.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/identity.rs``.

Four routes, all behind :func:`require_admin_dependency` (and
``tenant_scope`` once that middleware lands — the
:class:`AdminState.identity_store` is opened per-tenant at boot, so
handlers don't take a per-call tenant arg):

* ``GET  /admin/identity?limit=&offset=`` — paginated list of users in
  this tenant's identity graph.
* ``GET  /admin/identity/{user_id}`` — detail view; returns every alias
  bound to ``user_id``.
* ``POST /admin/identity/{user_id}/issue-phrase`` — issue a fresh
  verification phrase for a ``(channel, channel_user_id)`` the operator
  has confirmed maps to ``user_id``. 201 returns the phrase +
  RFC-3339 ``expires_at``.
* ``POST /admin/identity/merge`` — operator-driven manual merge.

Disabled gate: when ``state.identity_store`` is ``None`` every route
returns **503 ``identity_disabled``**.
"""

from __future__ import annotations

from datetime import timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from corlinman_identity import (
    ChannelAlias,
    IdentityError,
    InvalidInputError,
    UserId,
    UserNotFoundError,
)
from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class UserSummaryOut(BaseModel):
    """One row in the user list. Mirrors the Rust ``UserSummary``."""

    user_id: str
    display_name: str | None = None
    alias_count: int


class IdentityListOut(BaseModel):
    """``GET /admin/identity`` response. Mirrors the UI's
    ``IdentityListResponse`` (``ui/lib/api/identity.ts``)."""

    users: list[UserSummaryOut] = Field(default_factory=list)


class AliasOut(BaseModel):
    """One alias on the wire. ``created_at`` is an RFC-3339 string with
    a ``Z`` suffix to match the Rust ``time::format_description::well_known::Rfc3339``
    output."""

    channel: str
    channel_user_id: str
    user_id: str
    binding_kind: str
    created_at: str


class IdentityDetailOut(BaseModel):
    """``GET /admin/identity/{user_id}`` response."""

    user_id: str
    aliases: list[AliasOut] = Field(default_factory=list)


class IssuePhraseBody(BaseModel):
    """``POST /admin/identity/{user_id}/issue-phrase`` body."""

    channel: str
    channel_user_id: str


class IssuePhraseOut(BaseModel):
    """201 response of issue-phrase. The phrase is echoed back so the
    admin UI can present it to the operator."""

    phrase: str
    user_id: str
    expires_at: str


class MergeBody(BaseModel):
    """``POST /admin/identity/merge`` body. ``decided_by`` is the
    operator's username — retained for audit even though the
    audit-log surface itself ships later."""

    into_user_id: str
    from_user_id: str
    decided_by: str


class MergeOut(BaseModel):
    """200 response of merge."""

    surviving_user_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rfc3339_z(dt: Any) -> str:
    """Format a tz-aware :class:`datetime` (or stringly date) as RFC-3339
    with ``Z`` suffix. Falls back to the Unix-epoch sentinel string the
    Rust crate uses on format failure."""
    if isinstance(dt, str):
        return dt
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        iso = dt.isoformat()
        if iso.endswith("+00:00"):
            return iso[:-6] + "Z"
        return iso
    except Exception:
        return "1970-01-01T00:00:00Z"


def _identity_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "identity_disabled"},
    )


def _require_store(state: AdminState) -> Any:
    """Borrow the identity store off ``state`` or raise the 503 envelope.

    All four handlers funnel through this so the disabled gate is
    enforced exactly once per route.
    """
    if state.identity_store is None:
        raise _identity_disabled()
    return state.identity_store


def _user_not_found_404(user_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "user_id": user_id},
    )


def _invalid_input_400(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_input", "message": message},
    )


def _storage_error_500(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "storage_error", "message": str(exc)},
    )


def _alias_to_out(alias: ChannelAlias) -> AliasOut:
    return AliasOut(
        channel=alias.channel,
        channel_user_id=alias.channel_user_id,
        user_id=str(alias.user_id),
        binding_kind=alias.binding_kind.as_str(),
        created_at=_to_rfc3339_z(alias.created_at),
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/identity*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/identity",
        response_model=IdentityListOut,
        summary="Paginated user list",
    )
    async def list_users(
        state: Annotated[AdminState, Depends(get_admin_state)],
        limit: Annotated[int | None, Query(ge=1, le=200)] = None,
        offset: Annotated[int | None, Query(ge=0)] = None,
    ) -> IdentityListOut:
        store = _require_store(state)
        eff_limit = 50 if limit is None else int(limit)
        eff_offset = 0 if offset is None else int(offset)
        try:
            users = await store.list_users(eff_limit, eff_offset)
        except IdentityError as exc:
            raise _storage_error_500(exc) from exc
        return IdentityListOut(
            users=[
                UserSummaryOut(
                    user_id=str(u.user_id),
                    display_name=u.display_name,
                    alias_count=u.alias_count,
                )
                for u in users
            ]
        )

    @r.get(
        "/admin/identity/{user_id}",
        response_model=IdentityDetailOut,
        summary="Detail view + aliases for one user",
    )
    async def detail(
        user_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> IdentityDetailOut:
        store = _require_store(state)
        try:
            aliases = await store.aliases_for(UserId(user_id))
        except IdentityError as exc:
            raise _storage_error_500(exc) from exc
        if not aliases:
            # Mirrors the Rust behaviour: an Auto-bound user always has
            # ≥1 alias, so an empty list is the safe 404 trigger.
            raise _user_not_found_404(user_id)
        return IdentityDetailOut(
            user_id=user_id,
            aliases=[_alias_to_out(a) for a in aliases],
        )

    @r.post(
        "/admin/identity/{user_id}/issue-phrase",
        response_model=IssuePhraseOut,
        status_code=status.HTTP_201_CREATED,
        summary="Issue a verification phrase for a (channel, channel_user_id) pair",
    )
    async def issue_phrase(
        user_id: str,
        body: IssuePhraseBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> IssuePhraseOut:
        store = _require_store(state)
        if not body.channel.strip():
            raise _invalid_input_400("channel must be non-empty")
        if not body.channel_user_id.strip():
            raise _invalid_input_400("channel_user_id must be non-empty")
        if not user_id.strip():
            raise _invalid_input_400("user_id path segment must be non-empty")

        try:
            phrase = await store.issue_phrase(
                UserId(user_id), body.channel, body.channel_user_id
            )
        except InvalidInputError as exc:
            raise _invalid_input_400(str(exc)) from exc
        except IdentityError as exc:
            raise _storage_error_500(exc) from exc

        return IssuePhraseOut(
            phrase=phrase.phrase,
            user_id=user_id,
            expires_at=_to_rfc3339_z(phrase.expires_at),
        )

    @r.post(
        "/admin/identity/merge",
        response_model=MergeOut,
        summary="Operator-driven manual merge",
    )
    async def merge(
        body: MergeBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> MergeOut:
        store = _require_store(state)
        if not body.into_user_id.strip():
            raise _invalid_input_400("into_user_id must be non-empty")
        if not body.from_user_id.strip():
            raise _invalid_input_400("from_user_id must be non-empty")
        if not body.decided_by.strip():
            raise _invalid_input_400("decided_by must be non-empty")

        try:
            surviving = await store.merge_users(
                UserId(body.into_user_id),
                UserId(body.from_user_id),
                body.decided_by,
            )
        except InvalidInputError as exc:
            raise _invalid_input_400(str(exc)) from exc
        except UserNotFoundError as exc:
            raise _user_not_found_404(exc.user_id) from exc
        except IdentityError as exc:
            raise _storage_error_500(exc) from exc

        return MergeOut(surviving_user_id=str(surviving))

    return r


__all__ = [
    "AliasOut",
    "IdentityDetailOut",
    "IdentityListOut",
    "IssuePhraseBody",
    "IssuePhraseOut",
    "MergeBody",
    "MergeOut",
    "UserSummaryOut",
    "router",
]
