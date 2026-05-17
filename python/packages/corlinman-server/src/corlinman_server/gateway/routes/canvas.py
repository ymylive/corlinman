"""``/v1/canvas/*`` — Canvas Host endpoints.

Python port of ``rust/crates/corlinman-gateway/src/routes/canvas.rs``.
Mirrors the four route family the Rust file ships:

* ``POST /v1/canvas/session`` — allocate an in-memory canvas session.
* ``POST /v1/canvas/frame`` — push a frame event to a session
  (the ``present`` kind is enriched server-side by invoking
  :class:`corlinman_canvas.Renderer`).
* ``GET  /v1/canvas/session/{id}/events`` — Server-Sent Events stream.
* ``POST /v1/canvas/render`` — synchronous renderer (no session
  required; for Swift client previews + future static export).

Session state lives in-process behind an :class:`asyncio.Lock` —
matches the Rust ``Arc<RwLock<HashMap<...>>>`` shape adapted to
asyncio. A background sweeper task drops expired sessions roughly
once per second and notifies subscribed SSE streams so clients
close promptly.

Auth + config-gating in the Rust file are handled by sibling
middleware layers (``require_admin`` + ``[canvas] host_endpoint_enabled``);
the port surfaces an ``enabled`` flag on :class:`CanvasState` so
boot code can wire those decisions in from the live config. When
``enabled=False`` every handler returns 503 ``canvas_host_disabled``
— byte-equivalent to the Rust ``disabled_response``.

Legacy ``/canvas/*`` aliases keep existing producers (admin UI,
Swift) working through the port.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

__all__ = ["ALLOWED_FRAME_KINDS", "CanvasState", "router"]

ALLOWED_FRAME_KINDS: tuple[str, ...] = (
    "present",
    "hide",
    "navigate",
    "eval",
    "snapshot",
    "a2ui_push",
    "a2ui_reset",
)
"""Whitelist accepted on ``POST /v1/canvas/frame``. Anything else →
400 ``invalid_frame_kind``. Mirrors the Rust ``ALLOWED_FRAME_KINDS``
constant byte-for-byte.
"""

_SSE_KEEPALIVE_SECS = 15
_DEFAULT_SESSION_TTL_SECS = 600
_DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024
_PRESENT_DEDUPE_CAP = 1024


def _now_ms() -> int:
    """Wall-clock millis since the UNIX epoch."""
    return int(time.time() * 1000)


def _new_session_id() -> str:
    """``cs_`` + 8 lowercase hex chars. Matches the Rust id shape."""
    return "cs_" + uuid.uuid4().hex[:8]


@dataclass(slots=True)
class _CanvasEvent:
    event_id: str
    session_id: str
    kind: str
    payload: Any
    at_ms: int

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "payload": self.payload,
            "at_ms": self.at_ms,
        }


@dataclass(slots=True)
class _Session:
    title: str
    initial_state: Any
    expires_at_ms: int
    events: list[_CanvasEvent] = field(default_factory=list)
    # Per-session fanout queues. New subscribers grab a queue; published
    # events are pushed onto every live queue. ``None`` on a queue
    # signals expiry / shutdown.
    subscribers: list[asyncio.Queue[_CanvasEvent | None]] = field(default_factory=list)
    seen_present_keys: dict[str, None] = field(default_factory=dict)


@dataclass(slots=True)
class CanvasState:
    """Shared canvas state.

    Mirrors the Rust ``CanvasState`` struct adapted to asyncio +
    :class:`corlinman_canvas.Renderer`. Construct one per gateway and
    inject into :func:`router`.

    ``enabled`` is the Python equivalent of the Rust
    ``[canvas] host_endpoint_enabled`` flag — boot code reads it from
    the live config snapshot.

    The constructor does NOT auto-start the janitor; call
    :meth:`start_janitor` once the event loop is running.
    """

    enabled: bool = True
    session_ttl_secs: int = _DEFAULT_SESSION_TTL_SECS
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES
    renderer: Any = None  # corlinman_canvas.Renderer

    _sessions: dict[str, _Session] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _janitor: asyncio.Task[None] | None = field(default=None, init=False)

    async def start_janitor(self) -> None:
        """Spawn the background expiry sweeper."""
        if self._janitor is not None:
            return
        self._janitor = asyncio.create_task(self._janitor_loop())

    async def stop_janitor(self) -> None:
        """Cancel the sweeper at shutdown."""
        if self._janitor is None:
            return
        self._janitor.cancel()
        try:
            await self._janitor
        except (asyncio.CancelledError, BaseException):
            pass
        self._janitor = None

    async def _janitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                now = _now_ms()
                expired: list[str] = []
                async with self._lock:
                    for sid, sess in self._sessions.items():
                        if sess.expires_at_ms <= now:
                            expired.append(sid)
                    for sid in expired:
                        sess = self._sessions.pop(sid, None)
                        if sess is None:
                            continue
                        for q in sess.subscribers:
                            try:
                                q.put_nowait(None)
                            except asyncio.QueueFull:  # pragma: no cover — unbounded queue
                                pass
        except asyncio.CancelledError:
            return


def _disabled_response() -> JSONResponse:
    """Byte-equivalent to the Rust ``disabled_response``."""
    return JSONResponse(
        {
            "error": "canvas_host_disabled",
            "message": "Set [canvas] host_endpoint_enabled = true in config.toml",
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _render_error_metadata(err: Exception) -> tuple[str, str | None]:
    """Map a :class:`corlinman_canvas.CanvasError` to UI-stable
    (code, optional kind) — mirror of the Rust
    ``render_error_metadata`` helper. Falls back to generic codes
    when the canvas package isn't available.
    """
    try:
        from corlinman_canvas import (  # noqa: PLC0415
            AdapterError,
            BodyTooLarge,
            UnimplementedKind,
            UnknownKind,
        )
        from corlinman_canvas import TimeoutError_ as CanvasTimeout  # noqa: PLC0415
    except ImportError:
        return "adapter_error", None

    if isinstance(err, UnimplementedKind):
        return "unimplemented", getattr(err, "kind", None)
    if isinstance(err, UnknownKind):
        return "unknown_kind", None
    if isinstance(err, BodyTooLarge):
        return "body_too_large", getattr(err, "kind", None)
    if isinstance(err, CanvasTimeout):
        return "timeout", getattr(err, "kind", None)
    if isinstance(err, AdapterError):
        return "adapter_error", getattr(err, "kind", None)
    return "adapter_error", None


def router(state: CanvasState) -> APIRouter:
    """Build the ``/v1/canvas/*`` sub-router (plus legacy aliases)."""
    api = APIRouter()

    async def _create_session(request: Request) -> JSONResponse:
        if not state.enabled:
            return _disabled_response()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        ttl_secs = int(body.get("ttl_secs") or state.session_ttl_secs)
        ttl_secs = max(1, min(86_400, ttl_secs))
        created_at = _now_ms()
        session_id = _new_session_id()
        async with state._lock:
            state._sessions[session_id] = _Session(
                title=str(body.get("title") or "untitled"),
                initial_state=body.get("initial_state") or {},
                expires_at_ms=created_at + ttl_secs * 1000,
            )
        return JSONResponse(
            {
                "session_id": session_id,
                "created_at_ms": created_at,
                "expires_at_ms": created_at + ttl_secs * 1000,
            },
            status_code=status.HTTP_201_CREATED,
        )

    async def _post_frame(request: Request) -> JSONResponse:
        if not state.enabled:
            return _disabled_response()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("expected JSON object")
            session_id = str(body["session_id"])
            kind = str(body["kind"])
            payload = body.get("payload")
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse(
                {"error": "invalid_request", "message": str(exc)},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if kind not in ALLOWED_FRAME_KINDS:
            return JSONResponse(
                {
                    "error": "invalid_frame_kind",
                    "message": f"kind '{kind}' is not in the whitelist",
                    "allowed": list(ALLOWED_FRAME_KINDS),
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        enriched_payload = payload
        present_key: str | None = None
        render_warnings: list[str] | None = None

        if kind == "present" and state.renderer is not None:
            try:
                from corlinman_canvas import (  # noqa: PLC0415
                    CanvasError,
                    CanvasPresentPayload,
                )
            except ImportError:
                CanvasError = Exception  # type: ignore[assignment, misc]
                CanvasPresentPayload = None  # type: ignore[assignment]

            if CanvasPresentPayload is not None and isinstance(payload, dict):
                try:
                    parsed = (
                        CanvasPresentPayload.from_json(payload)
                        if hasattr(CanvasPresentPayload, "from_json")
                        else CanvasPresentPayload(**payload)
                    )
                except Exception:  # noqa: BLE001 — pass through verbatim
                    parsed = None

                if parsed is not None:
                    present_key = getattr(parsed, "idempotency_key", None)
                    # Body cap parity with /canvas/render
                    import json  # noqa: PLC0415

                    body_bytes = len(json.dumps(payload).encode())
                    if body_bytes > state.max_artifact_bytes:
                        return JSONResponse(
                            {
                                "error": "body_too_large",
                                "max_bytes": state.max_artifact_bytes,
                                "actual_bytes": body_bytes,
                            },
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        )
                    try:
                        artifact = state.renderer.render(parsed)
                        if hasattr(artifact, "warnings") and artifact.warnings:
                            render_warnings = list(artifact.warnings)
                        if isinstance(enriched_payload, dict):
                            enriched_payload = {
                                **enriched_payload,
                                "rendered": (
                                    artifact.to_json()
                                    if hasattr(artifact, "to_json")
                                    else artifact
                                ),
                            }
                    except CanvasError as exc:  # type: ignore[misc]
                        code, kind_name = _render_error_metadata(exc)
                        if isinstance(enriched_payload, dict):
                            enriched_payload = {
                                **enriched_payload,
                                "render_error": {
                                    "code": code,
                                    "message": str(exc),
                                    "artifact_kind": kind_name,
                                },
                            }

        event = _CanvasEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            kind=kind,
            payload=enriched_payload,
            at_ms=_now_ms(),
        )

        async with state._lock:
            session = state._sessions.get(session_id)
            if session is None or session.expires_at_ms <= _now_ms():
                return JSONResponse(
                    {"error": "session_not_found", "session_id": session_id},
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if present_key is not None:
                if present_key in session.seen_present_keys:
                    return JSONResponse(
                        {
                            "event_id": None,
                            "deduped": True,
                            "idempotency_key": present_key,
                        }
                    )
                if len(session.seen_present_keys) >= _PRESENT_DEDUPE_CAP:
                    session.seen_present_keys.clear()
                session.seen_present_keys[present_key] = None
            session.events.append(event)
            for q in session.subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover
                    pass

        out: dict[str, Any] = {"event_id": event.event_id}
        if render_warnings is not None:
            out["warnings"] = render_warnings
        if present_key is not None:
            out["idempotency_key"] = present_key
        return JSONResponse(out, status_code=status.HTTP_202_ACCEPTED)

    async def _stream_events(id: str) -> Response:  # noqa: A002
        if not state.enabled:
            return _disabled_response()
        async with state._lock:
            session = state._sessions.get(id)
            if session is None:
                return JSONResponse(
                    {"error": "session_not_found", "session_id": id},
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            queue: asyncio.Queue[_CanvasEvent | None] = asyncio.Queue()
            session.subscribers.append(queue)

        async def _agen() -> AsyncIterator[bytes]:
            import json  # noqa: PLC0415

            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(
                            queue.get(), timeout=_SSE_KEEPALIVE_SECS
                        )
                    except TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    if ev is None:
                        yield b"event: end\ndata: {}\n\n"
                        return
                    data = json.dumps(ev.to_json()).encode()
                    yield b"event: canvas\ndata: " + data + b"\n\n"
            finally:
                async with state._lock:
                    sess = state._sessions.get(id)
                    if sess is not None and queue in sess.subscribers:
                        sess.subscribers.remove(queue)

        return StreamingResponse(_agen(), media_type="text/event-stream")

    async def _render(request: Request) -> JSONResponse:
        if not state.enabled:
            return _disabled_response()
        if state.renderer is None:
            return JSONResponse(
                {
                    "error": "renderer_unavailable",
                    "message": "no Renderer wired on CanvasState",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": "invalid_request", "message": str(exc)},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        import json  # noqa: PLC0415

        body_bytes = len(json.dumps(payload).encode())
        if body_bytes > state.max_artifact_bytes:
            return JSONResponse(
                {
                    "error": "body_too_large",
                    "max_bytes": state.max_artifact_bytes,
                    "actual_bytes": body_bytes,
                },
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        try:
            from corlinman_canvas import (  # noqa: PLC0415
                CanvasError,
                CanvasPresentPayload,
            )
        except ImportError:
            return JSONResponse(
                {
                    "error": "renderer_unavailable",
                    "message": "corlinman_canvas not installed",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            parsed = (
                CanvasPresentPayload.from_json(payload)
                if hasattr(CanvasPresentPayload, "from_json")
                else CanvasPresentPayload(**payload)
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": "invalid_request", "message": str(exc)},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            artifact = state.renderer.render(parsed)
        except CanvasError as exc:
            code, kind_name = _render_error_metadata(exc)
            return JSONResponse(
                {
                    "error": code,
                    "message": str(exc),
                    "artifact_kind": kind_name,
                },
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        body = (
            artifact.to_json() if hasattr(artifact, "to_json") else artifact
        )
        return JSONResponse(body)

    # Canonical /v1 routes
    api.add_api_route("/v1/canvas/session", _create_session, methods=["POST"])
    api.add_api_route("/v1/canvas/frame", _post_frame, methods=["POST"])
    api.add_api_route(
        "/v1/canvas/session/{id}/events", _stream_events, methods=["GET"]
    )
    api.add_api_route("/v1/canvas/render", _render, methods=["POST"])

    # Legacy Rust-compatible aliases
    api.add_api_route("/canvas/session", _create_session, methods=["POST"])
    api.add_api_route("/canvas/frame", _post_frame, methods=["POST"])
    api.add_api_route(
        "/canvas/session/{id}/events", _stream_events, methods=["GET"]
    )
    api.add_api_route("/canvas/render", _render, methods=["POST"])

    return api
