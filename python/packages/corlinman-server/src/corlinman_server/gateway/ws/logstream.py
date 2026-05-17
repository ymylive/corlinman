"""Log stream WebSocket: ``GET /logstream?token=<token>``.

Port of :rust:`corlinman_gateway::ws::logstream`. The Rust file is
intentionally an empty design stub today; we keep parity so the eventual
implementation lands additively when the broadcast bus + tail/resume
semantics are wired in.

TODO (matches the Rust TODO list):
* authenticate via query string ``token`` or ``Authorization`` header;
  attach to the gateway events broadcast and forward as structured JSON
  frames.
* support tail/resume semantics so reconnects don't miss the last N
  messages.

When implemented the surface will expose:

* ``async def handle_logstream(websocket, events_broadcast, *, tail=None)``
  — the per-connection loop, suitable for plugging into a
  :class:`fastapi.WebSocketRoute`.
* ``def router() -> APIRouter`` — convenience helper mirroring the
  Rust ``pub fn router() -> Router``.
"""

from __future__ import annotations

__all__: list[str] = []
