"""Composition root: bundle every route sub-router onto one FastAPI APIRouter.

Python port of ``rust/crates/corlinman-gateway/src/routes/mod.rs``
(the ``router()`` / ``router_with_full_state*()`` helpers).

The Rust file ships four progressively richer composition helpers:

* ``router()`` — every sub-router in stub form (501 / empty-checks).
* ``router_with_chat_state(state)`` — chat + approve wired, rest stub.
* ``router_with_full_state(state, async_tasks)`` — adds plugin callback.
* ``router_with_full_state_and_health(state, async_tasks, health)`` —
  adds live health probes.

The Python port collapses those into one
:class:`GatewayState` carrier + two factory functions:

* :func:`build_app_router` — every sub-router, fed whatever fields
  of :class:`GatewayState` were set. Unset fields → that route's
  stub behaviour (501 / disabled).
* :func:`build_minimal_router` — health + metrics only, for boot
  ordering where the app needs to expose probes before the heavy
  collaborators are ready.

Usage::

    from fastapi import FastAPI
    from corlinman_server.gateway.routes.register import (
        GatewayState, build_app_router,
    )

    state = GatewayState(chat=chat_state, memory=memory_state, ...)
    app = FastAPI()
    app.include_router(build_app_router(state))
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter

from corlinman_server.gateway.routes import (
    canvas,
    channels,
    chat,
    chat_approve,
    embeddings,
    health,
    memory,
    metrics,
    models,
    plugin_callback,
)

__all__ = [
    "GatewayState",
    "build_app_router",
    "build_minimal_router",
]


@dataclass(slots=True)
class GatewayState:
    """Bundle every per-route state holder.

    Every field is optional; a ``None`` value tells the corresponding
    sub-router to fall back to its stub / disabled behaviour. This
    mirrors the Rust composition helpers' "you can boot without
    Python yet" ergonomics.
    """

    chat: chat.ChatState | None = None
    chat_approve: chat_approve.ChatApproveState | None = None
    memory: memory.MemoryState | None = None
    canvas: canvas.CanvasState | None = None
    channels_telegram: channels.TelegramWebhookState | None = None
    health: health.HealthState | None = None
    plugin_async_tasks: object | None = None  # AsyncTaskRegistry (lazy import)
    models_source: models.ModelSource | None = None
    embedder: embeddings.EmbedderFn | None = None
    metrics_registry: object | None = None  # prometheus CollectorRegistry


def build_app_router(state: GatewayState) -> APIRouter:
    """Compose every route sub-router into one :class:`APIRouter`.

    Mirrors the Rust ``router_with_full_state_and_health`` flow:
    each sub-module's ``router()`` factory is invoked once, then the
    results are merged into the returned router via
    :meth:`APIRouter.include_router`.
    """
    parent = APIRouter()

    # Health + metrics first — boot probes should land before /v1/* so
    # an early 404 on /v1/chat doesn't shadow the readiness signal.
    parent.include_router(health.router(state.health))
    parent.include_router(metrics.router(state.metrics_registry))

    # /v1 chat surface
    parent.include_router(chat.router(state.chat))
    parent.include_router(chat_approve.router(state.chat_approve))

    # /v1 ancillary
    parent.include_router(embeddings.router(state.embedder))
    parent.include_router(models.router(state.models_source))

    # Memory + canvas only when explicitly wired (they require real
    # adapter instances; no stub form mirrors useful production behaviour).
    if state.memory is not None:
        parent.include_router(memory.router(state.memory))
    if state.canvas is not None:
        parent.include_router(canvas.router(state.canvas))

    # Channel webhooks
    if state.channels_telegram is not None:
        parent.include_router(channels.router(state.channels_telegram))

    # Plugin callback — type-narrow the registry slot for lazy import safety.
    if state.plugin_async_tasks is not None:
        from corlinman_providers.plugins.async_task import (  # noqa: PLC0415
            AsyncTaskRegistry,
        )

        if not isinstance(state.plugin_async_tasks, AsyncTaskRegistry):
            raise TypeError(
                "GatewayState.plugin_async_tasks must be an AsyncTaskRegistry"
            )
        parent.include_router(plugin_callback.router(state.plugin_async_tasks))
    else:
        # Mount the stub form so the route surface stays predictable
        # for clients that probe it during boot.
        parent.include_router(plugin_callback.router(None))

    return parent


def build_minimal_router(
    health_state: health.HealthState | None = None,
    metrics_registry: object | None = None,
) -> APIRouter:
    """Health + metrics only — used by boot code that wants probe
    endpoints up before the chat plane is ready. Mirrors the Rust
    ``router()`` stub-form composition for the same purpose.
    """
    parent = APIRouter()
    parent.include_router(health.router(health_state))
    parent.include_router(metrics.router(metrics_registry))
    return parent
