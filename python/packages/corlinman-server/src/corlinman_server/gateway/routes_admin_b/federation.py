"""``/admin/federation/peers*`` — tenant federation peer admin surface.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/federation.rs``.

Four routes, all behind ``require_admin`` and tenant-scoped to the
caller's resolved tenant id. Every row read or written lives in the
root-level ``tenants.sqlite`` admin DB
(:class:`corlinman_server.tenancy.AdminDb`):

* ``GET    /admin/federation/peers``
  → ``{ accepted_from: FederationPeer[], peers_of_us: FederationPeer[] }``.
* ``POST   /admin/federation/peers``
  body ``{ source_tenant_id }`` → 201 with the inserted row's metadata.
* ``DELETE /admin/federation/peers/{source_tenant_id}``
  → ``{ removed: bool }``; 404 ``not_found`` when the pair didn't exist
  so the UI can render an inline error rather than silent success.
* ``GET    /admin/federation/peers/{source_tenant_id}/recent_proposals``
  → ``{ proposals: [...] }``. Reads the *current tenant*'s per-tenant
  ``evolution.sqlite`` and returns the last 50 federated proposals
  received from ``source_tenant_id``.

### Disabled / not-found paths

* **503 ``tenants_disabled``** when either ``[tenants].enabled = false``
  or :attr:`AdminState.admin_db` is ``None``. Same gate as
  ``/admin/tenants``; the UI keys off the status code to render the
  "multi-tenant federation is off" banner.
* **400 ``invalid_tenant_slug``** on bad body / path slugs.
* **404 ``not_found``** on DELETE when the row was absent. On the
  ``recent_proposals`` route a 404 is reserved for a malformed slug —
  an empty proposals array is the legitimate no-rows happy path.

### Tenant resolution

The Rust route uses an axum ``Tenant`` extractor backed by the
``tenant_scope`` middleware. The Python ``tenant_scope`` module is part
of a parallel scope; this port reads ``request.state.tenant`` (the
same slot the ``auth`` middleware writes to on Bearer / API-key
flows), falls back to ``TenantId.legacy_default()`` when nothing is
present. That keeps the route importable + serviceable in
single-tenant legacy deployments without coupling to a
yet-to-land middleware install order.

### ``accepted_by`` source

Best-effort extraction from the ``Authorization: Basic ...`` header
when present; falls back to ``"admin"`` otherwise. Mirrors the Rust
inline-parse helper rather than waiting on an ``AdminSession``
extractor. A future iteration can swap to ``request.state.admin_user``
once the admin session middleware grows that contract.

### ``corlinman_memory_host`` linkage

The ``corlinman_memory_host`` package ships :class:`FederatedMemoryHost`
for *runtime query* fan-out across peers; this admin surface manages
the *peer roster* the runtime federator consumes. The admin DB layer
is the single source of truth for "tenant A accepts from tenant B" —
the memory-host federator reads the same rows out-of-band when
deciding which peers to fan a query to.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Path as PathParam, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire shapes (pydantic v2)
# ---------------------------------------------------------------------------


class FederationPeerOut(BaseModel):
    """One row of ``tenant_federation_peers`` on the wire."""

    peer_tenant_id: str
    source_tenant_id: str
    accepted_at_ms: int
    accepted_by: str | None = None


class PeersListOut(BaseModel):
    """Both halves of the federation graph for the current tenant."""

    accepted_from: list[FederationPeerOut]
    peers_of_us: list[FederationPeerOut]


class AddPeerBody(BaseModel):
    source_tenant_id: str


class AddPeerOut(BaseModel):
    peer_tenant_id: str
    source_tenant_id: str
    accepted_at_ms: int
    accepted_by: str


class RemovePeerOut(BaseModel):
    removed: bool


class FederatedFromOut(BaseModel):
    tenant: str
    source_proposal_id: str
    hop: int


class FederatedProposalOut(BaseModel):
    id: str
    kind: str
    status: str
    created_at: int
    federated_from: FederatedFromOut


class RecentProposalsOut(BaseModel):
    proposals: list[FederatedProposalOut]


# ---------------------------------------------------------------------------
# Error envelopes (mirror the Rust JSON shapes byte-for-byte)
# ---------------------------------------------------------------------------


def _tenants_disabled() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "tenants_disabled"})


def _invalid_tenant_slug(slug: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_tenant_slug",
            "slug": slug,
            "reason": reason,
        },
    )


def _peer_not_found(source: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "not_found", "source_tenant_id": source},
    )


def _storage_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "storage_error", "message": message},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_admin_db(state: AdminState):
    """Resolve the typed :class:`AdminDb` handle or return the 503
    envelope. Mirrors the Rust ``require_admin_db`` gate — config-level
    ``tenants.enabled = false`` and "admin DB not wired" collapse onto
    the same envelope so the UI renders one banner."""
    cfg = config_snapshot(state)
    tenants_cfg = cfg.get("tenants") if isinstance(cfg, dict) else None
    enabled = bool((tenants_cfg or {}).get("enabled", False))
    if not enabled:
        return None, _tenants_disabled()
    db = state.admin_db
    if db is None:
        return None, _tenants_disabled()
    return db, None


def _resolve_data_dir(state: AdminState) -> Path:
    """Same precedence ladder the Rust route uses:
    ``AdminState.data_dir`` → ``$CORLINMAN_DATA_DIR`` → ``~/.corlinman``."""
    if state.data_dir is not None:
        return state.data_dir
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    home = Path.home() if hasattr(Path, "home") else None
    if home is not None:
        return home / ".corlinman"
    return Path(".corlinman")


def _admin_username(request: Request) -> str:
    """Best-effort extraction of the operator's username. Tries
    ``request.state.admin_user`` (if a later middleware writes it),
    falls back to the ``Authorization: Basic ...`` header, then the
    default ``"admin"`` string."""
    user = getattr(request.state, "admin_user", None) if hasattr(request, "state") else None
    if isinstance(user, str) and user:
        return user
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.startswith("Basic "):
        encoded = auth[6:].strip()
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
        except Exception:  # noqa: BLE001 — malformed header → fall back to default
            return "admin"
        if ":" in decoded:
            candidate = decoded.split(":", 1)[0]
            if candidate:
                return candidate
    return "admin"


def _resolve_tenant(request: Request) -> Any:
    """Resolve a :class:`TenantId` for the current request.

    Tries ``request.state.tenant`` first (set by the auth / tenant_scope
    middlewares), accepting either a :class:`TenantId` instance, a
    raw slug string, or anything with a ``.as_str()`` method. Falls
    back to :meth:`TenantId.legacy_default` so single-tenant gateways
    continue to work without the tenant middleware installed.

    Raises :class:`TenantIdError` on a malformed string — callers map
    that to a 400 envelope.
    """
    from corlinman_server.tenancy import TenantId  # noqa: PLC0415

    raw = getattr(request.state, "tenant", None) if hasattr(request, "state") else None
    if raw is None:
        return TenantId.legacy_default()
    if isinstance(raw, TenantId):
        return raw
    if hasattr(raw, "as_str"):
        # Already a typed newtype from a sibling tenancy implementation.
        return TenantId.new(raw.as_str())
    if isinstance(raw, str):
        if not raw:
            return TenantId.legacy_default()
        return TenantId.new(raw)
    return TenantId.legacy_default()


def _evolution_db_path(state: AdminState, tenant: Any) -> Path:
    """Per-tenant ``evolution.sqlite`` path. Mirrors the Rust
    ``tenant_db_path(data_dir, tenant, "evolution")`` helper."""
    from corlinman_server.tenancy import tenant_db_path  # noqa: PLC0415

    return tenant_db_path(_resolve_data_dir(state), tenant, "evolution")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:  # noqa: C901 — single APIRouter factory mirrors the Rust pattern
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "federation"])

    @r.get("/admin/federation/peers", response_model=PeersListOut)
    async def list_peers(request: Request):
        state = get_admin_state()
        db, err = _require_admin_db(state)
        if err is not None:
            return err

        try:
            tenant = _resolve_tenant(request)
        except Exception as exc:  # noqa: BLE001 — bad tenant slug
            return _invalid_tenant_slug(
                getattr(request.state, "tenant", "") or "",
                str(exc),
            )

        try:
            accepted_from = await db.list_federation_sources_for(tenant)
            peers_of_us = await db.list_federation_peers_of(tenant)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        out = PeersListOut(
            accepted_from=[
                FederationPeerOut(
                    peer_tenant_id=row.peer_tenant_id.as_str(),
                    source_tenant_id=row.source_tenant_id.as_str(),
                    accepted_at_ms=row.accepted_at_ms,
                    accepted_by=row.accepted_by,
                )
                for row in accepted_from
            ],
            peers_of_us=[
                FederationPeerOut(
                    peer_tenant_id=row.peer_tenant_id.as_str(),
                    source_tenant_id=row.source_tenant_id.as_str(),
                    accepted_at_ms=row.accepted_at_ms,
                    accepted_by=row.accepted_by,
                )
                for row in peers_of_us
            ],
        )
        return out

    @r.post("/admin/federation/peers", status_code=201, response_model=AddPeerOut)
    async def add_peer(body: AddPeerBody, request: Request):
        from corlinman_server.tenancy import TenantId, TenantIdError  # noqa: PLC0415

        state = get_admin_state()
        db, err = _require_admin_db(state)
        if err is not None:
            return err

        try:
            tenant = _resolve_tenant(request)
        except TenantIdError as exc:
            return _invalid_tenant_slug(
                getattr(request.state, "tenant", "") or "", str(exc)
            )

        try:
            source = TenantId.new(body.source_tenant_id)
        except TenantIdError as exc:
            return _invalid_tenant_slug(body.source_tenant_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _invalid_tenant_slug(body.source_tenant_id, str(exc))

        if source == tenant:
            # Self-peering is a logical operator error rather than an
            # idempotent re-add — flag it as a 400 so the UI can render
            # a helpful "you can't federate with yourself" hint. The
            # ``INSERT OR IGNORE`` would otherwise silently no-op.
            return _invalid_tenant_slug(
                tenant.as_str(),
                "self-peering is not allowed (source must differ from current tenant)",
            )

        accepted_by = _admin_username(request)

        try:
            await db.add_federation_peer(tenant, source, accepted_by)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        # Fetch back the actual row so the response carries the real
        # stored timestamp + accepted_by (idempotent re-adds preserve
        # the original stamp / user per the AdminDb contract).
        try:
            rows = await db.list_federation_sources_for(tenant)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        stored = next((r for r in rows if r.source_tenant_id == source), None)
        if stored is None:
            # Defensive — INSERT OR IGNORE + same-pool readback should
            # never fail. Surface as storage_error rather than panic.
            return _storage_error("readback found no row after add")

        return AddPeerOut(
            peer_tenant_id=tenant.as_str(),
            source_tenant_id=source.as_str(),
            accepted_at_ms=stored.accepted_at_ms,
            accepted_by=stored.accepted_by or accepted_by,
        )

    @r.delete(
        "/admin/federation/peers/{source_tenant_id}",
        response_model=RemovePeerOut,
    )
    async def remove_peer(
        request: Request,
        source_tenant_id: str = PathParam(...),
    ):
        from corlinman_server.tenancy import TenantId, TenantIdError  # noqa: PLC0415

        state = get_admin_state()
        db, err = _require_admin_db(state)
        if err is not None:
            return err

        try:
            tenant = _resolve_tenant(request)
        except TenantIdError as exc:
            return _invalid_tenant_slug(
                getattr(request.state, "tenant", "") or "", str(exc)
            )

        try:
            source = TenantId.new(source_tenant_id)
        except TenantIdError as exc:
            return _invalid_tenant_slug(source_tenant_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _invalid_tenant_slug(source_tenant_id, str(exc))

        try:
            removed = await db.remove_federation_peer(tenant, source)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        if not removed:
            return _peer_not_found(source_tenant_id)
        return RemovePeerOut(removed=True)

    @r.get(
        "/admin/federation/peers/{source_tenant_id}/recent_proposals",
        response_model=RecentProposalsOut,
    )
    async def recent_proposals(
        request: Request,
        source_tenant_id: str = PathParam(...),
    ):
        from corlinman_server.tenancy import TenantId, TenantIdError  # noqa: PLC0415

        state = get_admin_state()
        _db, err = _require_admin_db(state)
        if err is not None:
            return err

        try:
            tenant = _resolve_tenant(request)
        except TenantIdError as exc:
            return _invalid_tenant_slug(
                getattr(request.state, "tenant", "") or "", str(exc)
            )

        try:
            source = TenantId.new(source_tenant_id)
        except TenantIdError as exc:
            return _invalid_tenant_slug(source_tenant_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _invalid_tenant_slug(source_tenant_id, str(exc))

        evo_path = _evolution_db_path(state, tenant)
        if not evo_path.exists():
            # New tenant that hasn't received any federated proposals
            # yet — return the empty-list happy path rather than 503,
            # mirroring the Rust early-return.
            return RecentProposalsOut(proposals=[])

        # Open the per-tenant evolution store on-demand. The federation
        # route doesn't sit on a hot path; per-request open + close is
        # fine and avoids holding a per-tenant connection forever on
        # the admin gateway.
        try:
            from corlinman_evolution_store import EvolutionStore  # noqa: PLC0415
        except ImportError as exc:
            return _storage_error(f"corlinman_evolution_store unavailable: {exc}")

        try:
            store = await EvolutionStore.open(evo_path)
        except Exception as exc:  # noqa: BLE001 — open / schema apply failure
            return _storage_error(str(exc))

        try:
            sql = (
                "SELECT id, kind, status, created_at, metadata "
                "  FROM evolution_proposals "
                " WHERE json_extract(metadata, '$.federated_from.tenant') = ? "
                " ORDER BY created_at DESC "
                " LIMIT 50"
            )
            try:
                cursor = await store.conn.execute(sql, (source.as_str(),))
                try:
                    rows = await cursor.fetchall()
                finally:
                    await cursor.close()
            except Exception as exc:  # noqa: BLE001
                return _storage_error(str(exc))
        finally:
            try:
                await store.close()
            except Exception:  # noqa: BLE001 — close failure is non-fatal
                pass

        proposals: list[FederatedProposalOut] = []
        for row in rows:
            id_ = str(row[0])
            kind = str(row[1])
            status = str(row[2])
            created_at = int(row[3])
            raw_meta = row[4]
            if raw_meta is None:
                # Filtered-out by the WHERE clause in principle; defensive
                # skip avoids a 500 if a row's metadata is NULL despite
                # the json_extract filter (schema drift).
                continue
            try:
                blob = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            except Exception:  # noqa: BLE001 — malformed JSON: skip + carry on
                continue
            if not isinstance(blob, dict):
                continue
            fed_from = blob.get("federated_from")
            if not isinstance(fed_from, dict):
                continue
            try:
                federated_from = FederatedFromOut(
                    tenant=str(fed_from.get("tenant", "")),
                    source_proposal_id=str(fed_from.get("source_proposal_id", "")),
                    hop=int(fed_from.get("hop", 0)),
                )
            except Exception:  # noqa: BLE001 — shape mismatch: skip
                continue
            proposals.append(
                FederatedProposalOut(
                    id=id_,
                    kind=kind,
                    status=status,
                    created_at=created_at,
                    federated_from=federated_from,
                )
            )
        return RecentProposalsOut(proposals=proposals)

    return r
