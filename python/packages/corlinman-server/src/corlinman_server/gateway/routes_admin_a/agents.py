"""``/admin/agents*`` — filesystem scan + edit of ``<data_dir>/agents/*.md``.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/agents.rs``.

Three routes:

* ``GET  /admin/agents``              — list ``*.md`` files (shallow)
* ``GET  /admin/agents/{name}``       — read one file (UTF-8 body)
* ``POST /admin/agents/{name}``       — atomic write of a file body

Path-traversal defence is identical to the Rust version: the ``name``
segment must be a bare stem (no ``/``, ``\\`` or ``..``). The
``.new``-then-rename atomic write mirrors the Rust handler verbatim.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)


# ---------------------------------------------------------------------------
# Wire shapes — mirror the Rust ``AgentSummaryOut`` / ``AgentContent``.
# ---------------------------------------------------------------------------


class AgentSummaryOut(BaseModel):
    """One row in ``GET /admin/agents``."""

    name: str
    file_path: str
    bytes: int
    last_modified: str | None = None


class AgentContent(BaseModel):
    """Full body for ``GET /admin/agents/{name}``."""

    name: str
    file_path: str
    bytes: int
    last_modified: str | None
    content: str


class SaveAgentBody(BaseModel):
    """``POST /admin/agents/{name}`` body — full replacement content."""

    content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _system_time_to_rfc3339(mtime_seconds: float) -> str | None:
    """Mirror Rust ``system_time_to_rfc3339`` — produce an RFC-3339 / ISO-8601
    string in UTC, or ``None`` on overflow."""
    try:
        return (
            _dt.datetime.fromtimestamp(mtime_seconds, tz=_dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _rel_path_str(base: Path, full: Path) -> str:
    """Render ``full`` as ``agents/<rel>`` when possible, falling back
    to the absolute path. Matches Rust ``rel_path_str``."""
    try:
        rel = full.relative_to(base)
        return str(Path("agents") / rel)
    except ValueError:
        return str(full)


def _validate_agent_name(name: str) -> None:
    """Reject empty names, path separators, or any ``..`` segment.

    Raises ``HTTPException(400, invalid_name)`` mirroring Rust
    ``agent_path_or_build``.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_name",
                "message": (
                    "agent name must be a bare stem without path "
                    "separators or '..'"
                ),
            },
        )


def _agent_path_or_build(agents_dir: Path, name: str) -> Path:
    """Construct ``<agents_dir>/<name>.md`` after validation."""
    _validate_agent_name(name)
    return agents_dir / f"{name}.md"


def _resolve_agent_path(agents_dir: Path, name: str) -> Path:
    """Like :func:`_agent_path_or_build` but also asserts the file exists."""
    path = _agent_path_or_build(agents_dir, name)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "resource": "agent", "id": name},
        )
    return path


def _scan_agents(agents_dir: Path) -> list[AgentSummaryOut]:
    """Shallow walk of ``agents_dir`` returning each ``*.md`` file as a
    summary row. Missing directory → empty list (matches Rust)."""
    if not agents_dir.is_dir():
        return []
    rows: list[AgentSummaryOut] = []
    for entry in agents_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        rows.append(
            AgentSummaryOut(
                name=entry.stem,
                file_path=_rel_path_str(agents_dir, entry),
                bytes=st.st_size,
                last_modified=_system_time_to_rfc3339(st.st_mtime),
            )
        )
    # Stable sort by name so the UI's table doesn't shuffle.
    rows.sort(key=lambda r: r.name)
    return rows


def _agents_dir_for(state: AdminState) -> Path:
    """Resolve the ``agents/`` directory under the state's data dir."""
    base = state.data_dir if state.data_dir is not None else Path.cwd()
    return Path(base) / "agents"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/agents*``. Mounted by the parent
    :func:`corlinman_server.gateway.routes_admin_a.router` helper."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/agents",
        response_model=list[AgentSummaryOut],
        summary="List agent markdown files",
    )
    async def list_agents(  # noqa: D401 — wired as FastAPI handler
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[AgentSummaryOut]:
        return _scan_agents(_agents_dir_for(state))

    @r.get(
        "/admin/agents/{name}",
        response_model=AgentContent,
        summary="Read one agent markdown file",
    )
    async def get_agent(
        name: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> AgentContent:
        agents_dir = _agents_dir_for(state)
        path = _resolve_agent_path(agents_dir, name)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "resource": "agent", "id": name},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "read_failed", "message": str(exc)},
            ) from exc
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "not_utf8", "message": str(exc)},
            ) from exc
        st = path.stat()
        return AgentContent(
            name=name,
            file_path=_rel_path_str(agents_dir, path),
            bytes=st.st_size,
            last_modified=_system_time_to_rfc3339(st.st_mtime),
            content=content,
        )

    @r.post(
        "/admin/agents/{name}",
        summary="Atomic save of an agent markdown file",
    )
    async def save_agent(
        name: str,
        body: SaveAgentBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, object]:
        agents_dir = _agents_dir_for(state)
        path = _agent_path_or_build(agents_dir, name)
        try:
            agents_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "mkdir_failed", "message": str(exc)},
            ) from exc
        tmp = path.with_name(path.name + ".new")
        try:
            tmp.write_bytes(body.content.encode("utf-8"))
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc
        try:
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "rename_failed", "message": str(exc)},
            ) from exc
        st = path.stat()
        return {
            "status": "ok",
            "name": name,
            "file_path": _rel_path_str(agents_dir, path),
            "bytes": st.st_size,
            "last_modified": _system_time_to_rfc3339(st.st_mtime),
        }

    return r


__all__ = [
    "AgentContent",
    "AgentSummaryOut",
    "SaveAgentBody",
    "router",
]
