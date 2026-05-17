"""``/admin/scheduler*`` — cron job listing + manual trigger + history.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/scheduler.rs``.

Three routes:

* ``GET  /admin/scheduler/jobs`` — definitions from ``[[scheduler.jobs]]``
  in the active config. ``next_fire_at`` / ``last_status`` are null
  until the cron runtime publishes runtime data.
* ``POST /admin/scheduler/jobs/{name}/trigger`` — best-effort manual fire.
  Falls back to recording a ``status=not_wired`` history entry and
  returning 501 when no scheduler runtime is attached.
* ``GET  /admin/scheduler/history`` — newest-first ring-buffer history.

Reuses ``corlinman_server.scheduler.SchedulerHandle`` when available
(:attr:`AdminState.scheduler`). History is kept in a tiny in-process
ring buffer parked on :attr:`AdminState.extras["scheduler_history"]`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)


class JobOut(BaseModel):
    name: str
    cron: str
    timezone: str | None = None
    action_kind: str
    next_fire_at: str | None = None
    last_status: str | None = None


class HistoryEntry(BaseModel):
    job: str
    at: str
    source: str
    status: str
    message: str


class SchedulerHistory:
    """In-process ring buffer matching the Rust ``SchedulerHistory``.

    Capped at 100 entries. Push is fire-and-forget; readers get a
    snapshot via :meth:`snapshot`.
    """

    MAX = 100

    def __init__(self) -> None:
        self._buf: list[HistoryEntry] = []

    def push(self, entry: HistoryEntry) -> None:
        self._buf.append(entry)
        if len(self._buf) > self.MAX:
            del self._buf[: len(self._buf) - self.MAX]

    def snapshot(self) -> list[HistoryEntry]:
        return list(self._buf)


def _history(state: AdminState) -> SchedulerHistory:
    h = state.extras.get("scheduler_history")
    if isinstance(h, SchedulerHistory):
        return h
    new = SchedulerHistory()
    state.extras["scheduler_history"] = new
    return new


def _action_kind(action: Any) -> str:
    """Best-effort mapper from the config job-action dict to the
    Rust-side string label (`run_agent` / `run_tool` / `subprocess`)."""
    if isinstance(action, dict):
        for key in ("run_agent", "run_tool", "subprocess"):
            if key in action:
                return key
        if "kind" in action and isinstance(action["kind"], str):
            return action["kind"]
    return "unknown"


def _list_jobs_from_config(cfg: dict[str, Any]) -> list[JobOut]:
    out: list[JobOut] = []
    sched = cfg.get("scheduler") if isinstance(cfg, dict) else None
    jobs = (sched or {}).get("jobs") or []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        out.append(
            JobOut(
                name=str(j.get("name", "")),
                cron=str(j.get("cron", "")),
                timezone=j.get("timezone"),
                action_kind=_action_kind(j.get("action")),
                next_fire_at=None,
                last_status=None,
            )
        )
    return out


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "scheduler"])

    @r.get("/admin/scheduler/jobs", response_model=list[JobOut])
    async def list_jobs() -> list[JobOut]:
        cfg = dict(config_snapshot())
        return _list_jobs_from_config(cfg)

    @r.post("/admin/scheduler/jobs/{name}/trigger")
    async def trigger_job(name: str):
        state = get_admin_state()
        cfg = dict(config_snapshot())
        jobs = _list_jobs_from_config(cfg)
        if not any(j.name == name for j in jobs):
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "scheduler_job", "id": name},
            )

        history = _history(state)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # If a SchedulerHandle is attached and exposes a manual-trigger
        # method, prefer it; otherwise fall through to the not-wired
        # branch that the Rust admin route also takes.
        sched = state.scheduler
        if sched is not None and hasattr(sched, "trigger"):
            try:
                await sched.trigger(name)
            except Exception as exc:  # noqa: BLE001
                entry = HistoryEntry(
                    job=name,
                    at=now_iso,
                    source="manual",
                    status="error",
                    message=str(exc),
                )
                history.push(entry)
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "trigger_failed",
                        "message": str(exc),
                        "recorded": entry.model_dump(),
                    },
                )
            entry = HistoryEntry(
                job=name,
                at=now_iso,
                source="manual",
                status="ok",
                message="manual trigger dispatched to scheduler runtime",
            )
            history.push(entry)
            return {"ok": True, "recorded": entry.model_dump()}

        entry = HistoryEntry(
            job=name,
            at=now_iso,
            source="manual",
            status="not_wired",
            message=(
                "scheduler runtime is not yet wired; trigger attempt "
                "recorded in history"
            ),
        )
        history.push(entry)
        return JSONResponse(
            status_code=501,
            content={
                "error": "scheduler_not_wired",
                "message": entry.message,
                "recorded": entry.model_dump(),
            },
        )

    @r.get("/admin/scheduler/history", response_model=list[HistoryEntry])
    async def list_history() -> list[HistoryEntry]:
        state = get_admin_state()
        snap = _history(state).snapshot()
        snap.reverse()
        return snap

    return r


# ---------------------------------------------------------------------------
# Pure helper for tests — exposed so the test module can stamp records
# directly without depending on the dataclass internals.
# ---------------------------------------------------------------------------


def make_history_entry(job: str, status: str, source: str = "manual", message: str = "") -> HistoryEntry:
    return HistoryEntry(
        job=job,
        at=datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        source=source,
        status=status,
        message=message,
    )
