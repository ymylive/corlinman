"""``GET /healthz`` / ``GET /readyz`` — liveness + readiness probes.

Python port of ``rust/crates/corlinman-gateway/src/routes/health.rs``.
Mirrors the Rust contract:

* Runs a small fixed set of probes in parallel and aggregates their
  status (``ok`` / ``warn`` / ``fail``).
* Overall response status: ``ok`` (all probes ok) / ``degraded``
  (any warn) / ``unhealthy`` (any fail).
* Each probe is independent — a missing collaborator degrades to
  ``warn`` instead of failing the route.

Two HTTP endpoints are exposed (Rust ships a single ``/health``; the
port doubles up so we honour both Kubernetes-canonical names the task
brief asked for):

* ``GET /healthz`` — full probe sweep, 200 on ``ok`` / ``degraded``,
  503 on ``unhealthy``.
* ``GET /readyz`` — same probe sweep as ``/healthz`` (the Python
  plane doesn't have a separate "started" gate today). Production
  boot may swap this for a no-op once startup ordering lands.

The handler returns 200 even on ``degraded`` to match the Rust
behaviour — only ``unhealthy`` flips to 5xx so callers can
distinguish "running but losing a probe" from "broken".
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

__all__ = [
    "CheckEntry",
    "HealthResponse",
    "HealthState",
    "ProbeStatus",
    "router",
]


ProbeStatus = Literal["ok", "warn", "fail"]
"""Per-probe outcome. Mirrors the Rust ``ProbeStatus`` enum byte-for-byte
on the wire (lowercase string).
"""

_STATUS_ORDER: dict[ProbeStatus, int] = {"ok": 0, "warn": 1, "fail": 2}


def _package_version() -> str:
    """Best-effort version string for the health payload's ``version`` field.

    Falls back to ``"0.0.0"`` when the package metadata is unavailable
    (editable installs, tests run out of a fresh git checkout).
    """
    try:
        return importlib.metadata.version("corlinman-server")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


@dataclass(slots=True)
class CheckEntry:
    """Per-probe row in the health response. Mirrors the Rust
    ``CheckEntry`` struct (name + status + optional detail).
    """

    name: str
    status: ProbeStatus
    detail: str | None = None

    @classmethod
    def ok(cls, name: str, detail: str | None = None) -> CheckEntry:
        return cls(name=name, status="ok", detail=detail)

    @classmethod
    def warn(cls, name: str, detail: str) -> CheckEntry:
        return cls(name=name, status="warn", detail=detail)

    @classmethod
    def fail(cls, name: str, detail: str) -> CheckEntry:
        return cls(name=name, status="fail", detail=detail)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass(slots=True)
class HealthResponse:
    """Aggregated health payload returned by the handler.

    Mirrors the Rust ``HealthResponse`` JSON shape:
    ``{status, version, checks: [...]}``.
    """

    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    checks: list[CheckEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "checks": [c.to_json() for c in self.checks],
        }


# ─── Probe state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class HealthState:
    """Probe inputs supplied by the application's boot path.

    Every field is optional — a stub router built without state
    degrades to the Rust ``health_stub`` behaviour (empty checks list
    + overall ``ok``). When a field is set, the matching probe runs
    on every request.

    Mirrors the Rust ``HealthState`` struct, adapted to Python's
    available collaborators:

    * ``ping_chat_service`` — async callable that returns ``True`` if
      the in-process :class:`ChatService` can accept a request. Falls
      back to a ``warn`` row when unset.
    * ``ping_db`` — async callable that pings the SQLite session
      store (or equivalent). Failure → ``fail``.
    * ``ping_providers`` — async callable returning the number of
      loaded providers; zero providers → ``warn``.
    * ``plugin_registry_diagnostics`` — sync callable returning
      ``(plugin_count, diagnostics_count)``. Non-zero diagnostics →
      ``warn`` (matches the Rust ``probe_plugins`` semantics).
    """

    ping_chat_service: Callable[[], Awaitable[bool]] | None = None
    ping_db: Callable[[], Awaitable[bool]] | None = None
    ping_providers: Callable[[], Awaitable[int]] | None = None
    plugin_registry_diagnostics: Callable[[], tuple[int, int]] | None = None
    version: str | None = None


# ─── Probes ──────────────────────────────────────────────────────────


async def _probe_chat_service(state: HealthState) -> CheckEntry:
    if state.ping_chat_service is None:
        return CheckEntry.warn("chat_service", "no chat service wired")
    try:
        ok = await asyncio.wait_for(state.ping_chat_service(), timeout=0.5)
    except TimeoutError:
        return CheckEntry.fail("chat_service", "ping timed out (500ms)")
    except Exception as exc:  # noqa: BLE001 — propagate as fail status
        return CheckEntry.fail("chat_service", f"ping raised: {exc}")
    return (
        CheckEntry.ok("chat_service", "reachable")
        if ok
        else CheckEntry.fail("chat_service", "ping returned false")
    )


async def _probe_db(state: HealthState) -> CheckEntry:
    if state.ping_db is None:
        return CheckEntry.ok("db", "no db probe wired")
    try:
        ok = await asyncio.wait_for(state.ping_db(), timeout=0.5)
    except TimeoutError:
        return CheckEntry.fail("db", "ping timed out (500ms)")
    except Exception as exc:  # noqa: BLE001
        return CheckEntry.fail("db", f"ping raised: {exc}")
    return CheckEntry.ok("db", "pingable") if ok else CheckEntry.fail("db", "ping failed")


async def _probe_providers(state: HealthState) -> CheckEntry:
    if state.ping_providers is None:
        return CheckEntry.warn("providers", "no provider registry wired")
    try:
        count = await asyncio.wait_for(state.ping_providers(), timeout=0.5)
    except TimeoutError:
        return CheckEntry.fail("providers", "ping timed out (500ms)")
    except Exception as exc:  # noqa: BLE001
        return CheckEntry.fail("providers", f"ping raised: {exc}")
    if count <= 0:
        return CheckEntry.warn("providers", "zero providers loaded")
    return CheckEntry.ok("providers", f"{count} provider(s) loaded")


def _probe_plugins(state: HealthState) -> CheckEntry:
    if state.plugin_registry_diagnostics is None:
        return CheckEntry.ok("plugin_registry", "no registry wired")
    try:
        count, diag = state.plugin_registry_diagnostics()
    except Exception as exc:  # noqa: BLE001
        return CheckEntry.fail("plugin_registry", f"diagnostics failed: {exc}")
    if diag == 0:
        return CheckEntry.ok("plugin_registry", f"{count} plugin(s); 0 diagnostics")
    return CheckEntry.warn("plugin_registry", f"{count} plugin(s); {diag} diagnostic(s)")


async def run_checks(state: HealthState) -> list[CheckEntry]:
    """Run every wired probe and return the entries in deterministic order."""
    # Run async probes concurrently — matches the Rust
    # `entries.push(probe_*).await` chain but without blocking the
    # event loop on slow probes.
    chat, db, prov = await asyncio.gather(
        _probe_chat_service(state),
        _probe_db(state),
        _probe_providers(state),
    )
    plugins = _probe_plugins(state)
    return [chat, db, prov, plugins]


def overall_status(entries: list[CheckEntry]) -> Literal["ok", "degraded", "unhealthy"]:
    """Worst-result aggregation. Mirrors the Rust ``overall_status`` helper."""
    if not entries:
        return "ok"
    worst = max(_STATUS_ORDER[e.status] for e in entries)
    if worst == 0:
        return "ok"
    if worst == 1:
        return "degraded"
    return "unhealthy"


# ─── Router ──────────────────────────────────────────────────────────


def router(state: HealthState | None = None) -> APIRouter:
    """Build the ``/healthz`` + ``/readyz`` sub-router.

    When ``state`` is ``None`` the router behaves like the Rust
    ``health_stub`` — every request returns ``{status: "ok",
    checks: []}`` with the current package version. Boot code with a
    real ``HealthState`` should pass it in so the live probes run.
    """
    api = APIRouter()
    effective = state if state is not None else HealthState()
    version = effective.version or _package_version()

    async def _handle() -> Response:
        if state is None:
            return JSONResponse(
                HealthResponse(status="ok", version=version, checks=[]).to_json()
            )
        checks = await run_checks(effective)
        body = HealthResponse(
            status=overall_status(checks),
            version=version,
            checks=checks,
        )
        http_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if body.status == "unhealthy"
            else status.HTTP_200_OK
        )
        return JSONResponse(body.to_json(), status_code=http_status)

    @api.get("/healthz")
    async def healthz() -> Response:  # noqa: D401 — FastAPI handler
        """Aggregate liveness probes."""
        return await _handle()

    @api.get("/readyz")
    async def readyz() -> Response:  # noqa: D401
        """Aggregate readiness probes — same body as ``/healthz`` today."""
        return await _handle()

    return api
