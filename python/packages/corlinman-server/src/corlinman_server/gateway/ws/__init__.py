"""``corlinman_server.gateway.ws`` — WebSocket helpers hosted by the gateway.

Mirrors :rust:`corlinman_gateway::ws`. The Rust crate is a placeholder
today (one ``pub mod logstream;`` declaration plus a doc-only
``logstream.rs`` describing future wiring) — the Python port preserves
the same shape: a ``logstream`` submodule with the documented design
intent in place so the eventual implementation lands additively without
moving the file around.

When the real ``/logstream`` handler is implemented the surface will
expose a :func:`router` helper returning a `starlette`/`fastapi`
``APIRouter`` (mirroring the Rust ``pub fn router() -> Router``
TODO in ``ws/mod.rs``).
"""

from __future__ import annotations

from corlinman_server.gateway.ws import logstream

__all__ = ["logstream"]
