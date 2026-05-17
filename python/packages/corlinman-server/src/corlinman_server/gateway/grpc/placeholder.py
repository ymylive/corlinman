"""gRPC wrapper over a Python ``PlaceholderEngine``.

Port of :rust:`corlinman_gateway::grpc::placeholder`. Direction: a
client (the Rust subsystem, the admin shell, the future Python
``context_assembler``) dials this service on the UDS path in
``$CORLINMAN_UDS_PATH`` (default ``/tmp/corlinman.sock``) and calls
``Render`` for every template it wants expanded before a provider call.

The :class:`PlaceholderEngine` Python sibling has not landed yet (it's
the W3 port of ``corlinman-core::placeholder``); we accept a structural
:class:`PlaceholderEngineLike` protocol so this module is testable today
and the eventual concrete engine drops in without touching this file.

Tokens with a namespace that has no resolver round-trip back unchanged
and are surfaced in ``RenderResponse.unresolved_keys`` for observability
— same contract as the Rust ``collect_unresolved`` post-render scan.

Error mapping preserves the enum shape of the Rust ``PlaceholderError``
so a single client library can dial either implementation:

==========================  =========================
engine error                ``error`` string
==========================  =========================
``CycleError(k)``           ``"cycle:<k>"``
``DepthExceededError(...)`` ``"depth_exceeded"``
``ResolverError(ns, msg)``  ``"resolver:<msg>"``
==========================  =========================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import grpc
from corlinman_grpc._generated.corlinman.v1 import (
    placeholder_pb2,
    placeholder_pb2_grpc,
)

__all__ = [
    "DEFAULT_RUST_SOCKET",
    "ENV_RUST_SOCKET",
    "PlaceholderCtx",
    "PlaceholderEngineLike",
    "PlaceholderError",
    "PlaceholderService",
    "collect_unresolved",
    "encode_error",
    "serve",
]


log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_RUST_SOCKET: str = "/tmp/corlinman.sock"
"""Default UDS path the gateway binds for Python→gateway traffic.

Kept separate from ``/tmp/corlinman-py.sock`` (the agent socket) so the
two sides can be restarted independently without stepping on each
other's socket file. Mirrors the Rust ``DEFAULT_RUST_SOCKET`` constant.
"""

ENV_RUST_SOCKET: str = "CORLINMAN_UDS_PATH"
"""Env var the Python ``PlaceholderClient`` honours, and the server
respects when set."""


# Mirrors the Rust ``TOKEN_RE`` lazy regex — same shape so the post-render
# unresolved-key scan finds the same tokens the engine would have tried
# to expand.
_TOKEN_RE: re.Pattern[str] = re.compile(r"\{\{([^{}]*?)\}\}")


# ─── Engine protocol (PlaceholderEngine port stub) ───────────────────


class PlaceholderCtx:
    """Render-time context handed to every resolver.

    Mirrors :rust:`corlinman_core::placeholder::PlaceholderCtx`. The
    actual Python ``PlaceholderEngine`` will own a richer version of
    this type; we keep a minimal shim here so the bridge can be
    constructed and tested without a hard dep on the (unported) engine.
    """

    __slots__ = ("session_key", "model_name", "metadata")

    def __init__(
        self,
        session_key: str,
        *,
        model_name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.session_key = session_key
        self.model_name = model_name
        self.metadata: dict[str, str] = dict(metadata or {})


class PlaceholderError(Exception):
    """Base class for the three documented placeholder error shapes."""


class CycleError(PlaceholderError):
    """Cycle detected at ``key``."""

    def __init__(self, key: str) -> None:
        super().__init__(f"placeholder cycle detected at key '{key}'")
        self.key = key


class DepthExceededError(PlaceholderError):
    """Recursion depth limit reached."""

    def __init__(self, depth: int) -> None:
        super().__init__(f"placeholder recursion depth {depth} exceeded")
        self.depth = depth


class ResolverError(PlaceholderError):
    """Resolver raised for ``namespace``."""

    def __init__(self, namespace: str, message: str) -> None:
        super().__init__(f"resolver for '{namespace}' failed: {message}")
        self.namespace = namespace
        self.message = message


@runtime_checkable
class PlaceholderEngineLike(Protocol):
    """Structural surface the bridge needs.

    Mirrors the slice of the Rust ``PlaceholderEngine`` API used by the
    gRPC wrapper. Concrete impls will land alongside the
    ``PlaceholderEngine`` Python port; tests can wire in a fake.
    """

    async def render(self, template: str, ctx: PlaceholderCtx) -> str: ...

    def clone_with_max_depth(self, max_depth: int) -> PlaceholderEngineLike: ...


# ─── Service ──────────────────────────────────────────────────────────


class PlaceholderService(placeholder_pb2_grpc.PlaceholderServicer):
    """gRPC service shell.

    Wraps a shared :class:`PlaceholderEngineLike` so multiple concurrent
    ``Render`` RPCs share the same resolver registry. The engine is
    accepted as ``Optional`` so callers can stand up a no-resolver
    service for tests / boot-time bridges where every token round-trips
    back through ``unresolved_keys``.
    """

    def __init__(self, engine: PlaceholderEngineLike | None) -> None:
        self._engine = engine

    @classmethod
    def with_empty_engine(cls) -> PlaceholderService:
        """Convenience for tests + the equivalent of the Rust
        ``PlaceholderService::with_empty_engine``.

        Returns a service whose engine echoes every template back
        verbatim (i.e. no resolvers registered). Every ``{{ns.name}}``
        token is surfaced via ``unresolved_keys``.
        """
        return cls(_NullEngine())

    async def Render(  # noqa: N802 — gRPC casing
        self,
        request: placeholder_pb2.RenderRequest,
        context: grpc.aio.ServicerContext,
    ) -> placeholder_pb2.RenderResponse:
        # Re-hydrate the engine context. The proto message allows an
        # empty ``model_name`` to mean "none"; the Python ctx encodes
        # that as ``None`` so round-trip the sentinel.
        ctx_msg = request.ctx
        ctx = PlaceholderCtx(
            session_key=ctx_msg.session_key if ctx_msg is not None else "",
            model_name=(ctx_msg.model_name or None) if ctx_msg is not None else None,
            metadata=dict(ctx_msg.metadata) if ctx_msg is not None else None,
        )

        # Honour per-call ``max_depth`` override. 0 = use engine default
        # (matches the proto docstring + the Rust branch).
        engine = self._engine
        if engine is None:
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error="resolver:engine not configured",
            )
        if request.max_depth != 0:
            engine = engine.clone_with_max_depth(int(request.max_depth))

        try:
            rendered = await engine.render(request.template, ctx)
        except PlaceholderError as err:
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error=encode_error(err),
            )
        except Exception as err:  # noqa: BLE001 — surface as resolver error
            # Unknown shapes — surface verbatim so the client can still
            # log something actionable. Mirrors the Rust ``encode_error``
            # fallback branch.
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error=f"resolver:{err}",
            )

        unresolved = collect_unresolved(rendered)
        return placeholder_pb2.RenderResponse(
            rendered=rendered,
            unresolved_keys=unresolved,
            error="",
        )


class _NullEngine:
    """Engine sibling of :rust:`PlaceholderEngine::new()` with zero
    resolvers — every template echoes back verbatim so the post-render
    scan surfaces every token as unresolved."""

    async def render(self, template: str, ctx: PlaceholderCtx) -> str:
        return template

    def clone_with_max_depth(self, max_depth: int) -> _NullEngine:
        return self


# ─── Helpers (pure) ───────────────────────────────────────────────────


def encode_error(err: Exception) -> str:
    """Encode a Python placeholder error back into the stable wire form.

    Mirrors :rust:`encode_error` byte-for-byte:

    * :class:`CycleError`        → ``"cycle:<k>"``
    * :class:`DepthExceededError`→ ``"depth_exceeded"``
    * :class:`ResolverError`     → ``"resolver:<msg>"``
    * unknown / generic          → ``"resolver:<str(err)>"``
    """
    if isinstance(err, CycleError):
        return f"cycle:{err.key}"
    if isinstance(err, DepthExceededError):
        return "depth_exceeded"
    if isinstance(err, ResolverError):
        return f"resolver:{err.message}"
    # Tolerate "wrapped" errors coming up through a future
    # ``CorlinmanError::Parse`` lookalike: match the prefixes the Rust
    # encoder strips before classifying.
    raw = str(err)
    inner = raw.removeprefix("parse error (placeholder): ")

    if inner.startswith("placeholder cycle detected at key '") and inner.endswith("'"):
        key = inner[len("placeholder cycle detected at key '") : -1]
        return f"cycle:{key}"
    if inner.startswith("placeholder recursion depth "):
        return "depth_exceeded"
    if inner.startswith("resolver for '"):
        # "resolver for '<ns>' failed: <inner>"
        rest = inner[len("resolver for '") :]
        marker = "' failed: "
        if marker in rest:
            _, tail = rest.split(marker, 1)
            return f"resolver:{tail}"

    return f"resolver:{inner}"


def collect_unresolved(rendered: str) -> list[str]:
    """Harvest still-literal ``{{…}}`` tokens from a rendered template.

    The engine preserves unknown tokens verbatim, so a post-render scan
    is the cheapest way to surface them without modifying the engine.
    Mirrors :rust:`collect_unresolved` 1:1, including the
    empty-body skip (``{{}}`` / ``{{ }}`` are intentionally preserved
    so callers can use them as literal markup).
    """
    if "{{" not in rendered:
        return []
    out: list[str] = []
    for match in _TOKEN_RE.finditer(rendered):
        body = match.group(1).strip()
        if not body:
            continue
        if body not in out:
            out.append(body)
    return out


# ─── Server helper ────────────────────────────────────────────────────


async def serve(
    socket_path: str | os.PathLike[str],
    service: PlaceholderService,
    shutdown: asyncio.Event | Awaitable[None],
) -> None:
    """Bind a ``grpc.aio`` server onto ``socket_path`` and serve the
    ``Placeholder`` service until ``shutdown`` fires.

    Removes the socket file on exit so subsequent boots can rebind
    cleanly. Mirrors :rust:`serve` — the call is non-fatal in spirit:
    callers wrap it in a task and log-and-continue if binding fails
    (e.g. permission denied on a read-only fs).

    ``shutdown`` may be either an :class:`asyncio.Event` (set when ready
    to stop) or any awaitable that resolves when the server should
    shut down. Mirrors the Rust ``F: Future<Output = ()>`` bound.
    """
    path = Path(os.fspath(socket_path))

    # Best-effort cleanup of a stale socket — a previous crash may have
    # left the file behind. Matches the Rust cleanup-before-bind dance.
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    server = grpc.aio.server()
    placeholder_pb2_grpc.add_PlaceholderServicer_to_server(service, server)
    # gRPC supports ``unix:`` URIs for UDS listeners.
    server.add_insecure_port(f"unix:{path}")
    await server.start()
    log.info("placeholder gRPC bound socket=%s", path)

    try:
        if isinstance(shutdown, asyncio.Event):
            await shutdown.wait()
        else:
            await shutdown
    finally:
        # Mirror the Rust ``serve_with_incoming_shutdown`` cleanup: try
        # a graceful stop first, then unlink the socket file.
        await server.stop(grace=1.0)
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()


# Re-export for typing convenience (matches Rust ``pub use`` pattern).
_unused_typing: tuple[Any, ...] = (
    Callable,
)  # keep imports flake-clean across linters that strip unused.
