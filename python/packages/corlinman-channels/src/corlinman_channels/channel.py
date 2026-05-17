"""Shared :class:`Channel` Protocol + :class:`ChannelRegistry` (Python port).

Python port of ``rust/.../channel.rs``. The two existing adapters
(:func:`corlinman_channels.onebot.OneBotAdapter`,
:func:`corlinman_channels.telegram.TelegramAdapter`) were wired ad-hoc
from the gateway. This module extracts a uniform contract so:

1. New inbound transports follow a single Protocol.
2. The gateway spawns every enabled channel via one iteration
   (:func:`spawn_all`) instead of bespoke helpers.
3. Per-channel behaviour is unchanged — :class:`QqChannel` /
   :class:`TelegramChannel` are thin wrappers around the existing
   adapters.

## Why the contract is minimal

The Rust spec sketched ``send`` / ``edit`` / ``typing`` / ``send_media``
methods on the trait, but today the reply path lives *inside* each
adapter (OneBot owns the action channel; Telegram owns the reply mpsc).
Exposing those as Protocol methods now would require tearing out both
adapters' internals — a change the parent task explicitly forbids
("DO NOT touch onebot.py or telegram.py internals — wrap them"). So
the Protocol exposes only the stable surface the gateway actually
consumes (``id``, ``enabled``, ``run``); outbound helpers can be added
later with default :class:`ChannelError.Unsupported` impls.

## Deliberate deviations from Rust

- Rust uses ``Arc<Config>`` everywhere; Python uses a structural type
  (any object with the right attributes; tests pass plain
  ``SimpleNamespace``). Avoids a hard dependency on a corlinman-core
  Python port that doesn't exist yet.
- Rust returns ``JoinHandle<Result<()>>``; Python returns
  ``asyncio.Task[None]`` and the task itself surfaces ``Exception`` on
  ``await`` (matches asyncio idiom).
- Rust's ``CancellationToken`` becomes :class:`asyncio.Event` — call
  ``cancel.set()`` to request shutdown; the adapter's ``run``
  coroutine should observe and exit promptly.
- Rust's ``async_trait`` becomes ``typing.Protocol`` with an
  ``async def run``. Python's ABCs would also work; we pick Protocol
  for structural-typing parity with the rest of the package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from corlinman_channels.common import UnsupportedError

__all__ = [
    "ApnsChannel",
    "Channel",
    "ChannelContext",
    "ChannelError",
    "ChannelRegistry",
    "QqChannel",
    "TelegramChannel",
    "spawn_all",
]


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------


class ChannelError(Exception):
    """Base error for channel-Protocol operations.

    Mirrors Rust's ``ChannelError`` enum (only the ``Unsupported``
    variant is in use today; see module docs for the rationale). Kept
    as a subclass-friendly base so future variants (``Transport``,
    ``Auth``) can land without changing the import surface.
    """

    @staticmethod
    def unsupported(operation: str) -> ChannelError:
        """Factory matching Rust ``ChannelError::Unsupported(op)``.

        Returns a :class:`UnsupportedError` (the existing
        :mod:`corlinman_channels.common` variant) with a stable
        ``"operation <op> not supported by this channel"`` message —
        downstream code can pattern-match on ``isinstance(..,
        UnsupportedError)`` or string-contains.
        """
        return UnsupportedError(f"operation {operation} not supported by this channel")


# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelContext:
    """Per-channel runtime handle shared by the gateway at spawn time.

    Mirrors the Rust ``ChannelContext``. ``config`` is intentionally
    typed as ``Any`` because the corlinman-core Python port doesn't
    exist yet — callers pass a ``SimpleNamespace`` (or real config
    object) whose ``.channels.{qq,telegram,apns}`` attribute the
    built-in channels read in :meth:`Channel.enabled`.

    All fields are cheap to copy by reference; the dataclass is
    re-usable across every spawned task without an explicit ``clone()``.
    """

    config: Any
    """Full config snapshot; each adapter pulls its own ``channels.*``
    sub-section inside :meth:`Channel.enabled` + :meth:`Channel.run`."""

    chat_service: Any = None
    """Shared chat pipeline the gateway built on top of its
    ``ChatBackend``. Typed ``Any`` for the same reason as ``config``.
    May be ``None`` in trait-impl tests that never dispatch."""

    model: str = ""
    """Default model id for channels whose inbound events carry no
    model hint."""

    rate_limit_hook: Any = None
    """Optional observation hook fired by the router each time a
    message is dropped by a rate-limit check. ``None`` in tests;
    populated in prod where the gateway wires it to Prometheus."""

    hook_bus: Any = None
    """Optional shared :class:`corlinman_hooks.HookBus`. Threaded
    through to the router so rate-limit rejections surface on
    ``HookEvent.RateLimitTriggered`` in addition to the legacy
    callback."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Channel(Protocol):
    """Inbound channel adapter contract.

    Implementations are constructed once at gateway boot and stored in
    a :class:`ChannelRegistry`. For each enabled channel, the gateway
    calls :meth:`run` on a dedicated task; the coroutine must honour
    the ``cancel`` :class:`asyncio.Event` so shutdown drains in bounded
    time.

    The Protocol is intentionally minimal — see the module docs for
    why outbound helpers aren't exposed here.
    """

    def id(self) -> str:
        """Short stable id (``"qq"``, ``"telegram"``). Used for
        logging, metric labels, and registry lookup."""
        ...

    def display_name(self) -> str:
        """Human-readable name for admin UI / logs. Defaults to
        :meth:`id`."""
        ...

    def enabled(self, cfg: Any) -> bool:
        """Whether this channel is enabled for ``cfg``. Called once
        per boot by :func:`spawn_all`."""
        ...

    async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
        """Run the adapter to completion or cancellation. ``return``
        cleanly on graceful exit; raise for fatal configuration or
        transport errors so the caller can surface them."""
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelRegistry:
    """Ordered set of :class:`Channel` impls the gateway will try to
    spawn at boot.

    Order is insertion order; :meth:`builtin` preserves ``qq →
    telegram`` (matches the pre-refactor call order so log output
    stays identical to Rust).
    """

    _channels: list[Channel] = field(default_factory=list)

    @classmethod
    def builtin(cls) -> ChannelRegistry:
        """Registry pre-populated with the built-in adapters:
        ``qq`` → ``telegram``."""
        r = cls()
        r.push(QqChannel())
        r.push(TelegramChannel())
        return r

    def push(self, ch: Channel) -> None:
        """Append an adapter. External callers (future Discord / Slack
        adapters) can push their own impls before :func:`spawn_all`."""
        self._channels.append(ch)

    def iter(self) -> Iterator[Channel]:
        """Iterate registered adapters in insertion order."""
        return iter(self._channels)

    def __iter__(self) -> Iterator[Channel]:
        # Make the registry directly iterable too — feels natural in Python.
        return iter(self._channels)

    def __len__(self) -> int:
        return len(self._channels)

    def is_empty(self) -> bool:
        """True when no adapters are registered. Mirrors Rust
        ``ChannelRegistry::is_empty``."""
        return not self._channels


# ---------------------------------------------------------------------------
# spawn_all
# ---------------------------------------------------------------------------


def spawn_all(
    registry: ChannelRegistry,
    ctx: ChannelContext,
    cancel: asyncio.Event,
) -> list[asyncio.Task[None]]:
    """Spawn one task per enabled channel and return the task handles.

    Disabled channels (:meth:`Channel.enabled` returns ``False``) are
    skipped without spawning; the returned list's length matches the
    enabled count. Each task awaits the channel's ``run`` coroutine
    so the caller can ``await`` to surface per-channel failures on
    shutdown.

    Unlike Rust where each spawn gets a child cancel token, here we
    share the same :class:`asyncio.Event` because Python's asyncio
    doesn't have a built-in parent/child cancellation hierarchy — the
    common practice is to let the same event be the shutdown flag for
    every spawned task. Callers that need per-task cancellation can
    pass distinct events themselves.
    """
    tasks: list[asyncio.Task[None]] = []
    for ch in registry.iter():
        if not ch.enabled(ctx.config):
            continue
        # Snapshot the dataclass so each adapter has its own logical
        # copy (matches Rust ``ctx.clone()`` per spawn). The fields are
        # shared by reference; only the wrapper is new.
        local_ctx = ChannelContext(
            config=ctx.config,
            chat_service=ctx.chat_service,
            model=ctx.model,
            rate_limit_hook=ctx.rate_limit_hook,
            hook_bus=ctx.hook_bus,
        )
        task = asyncio.create_task(
            ch.run(local_ctx, cancel),
            name=f"channel-{ch.id()}",
        )
        tasks.append(task)
    return tasks


# ---------------------------------------------------------------------------
# Built-in adapters — thin wrappers
# ---------------------------------------------------------------------------


def _cfg_get(cfg: Any, *path: str) -> Any:
    """Walk ``cfg.path[0].path[1]...`` with ``None`` short-circuiting.

    The Rust crate reaches into a typed ``Config`` struct; the Python
    plane uses a duck-typed ``cfg`` so we walk it defensively here.
    Returns ``None`` on any missing/None step so the ``enabled``
    helpers can express their checks as
    ``_cfg_get(cfg, "channels", "qq", "enabled") is True``.
    """
    cur = cfg
    for step in path:
        if cur is None:
            return None
        # Support both attribute access (SimpleNamespace) and mapping
        # access (TOML dicts).
        cur = cur.get(step) if isinstance(cur, dict) else getattr(cur, step, None)
    return cur


class QqChannel:
    """QQ / OneBot v11 adapter wrapper.

    Forwards :meth:`run` to the QQ orchestration helper in
    :mod:`corlinman_channels.service`. Behaviour stays bit-for-bit
    compatible with the pre-refactor ``run_qq_channel`` call path.
    """

    def id(self) -> str:
        return "qq"

    def display_name(self) -> str:
        return "QQ (OneBot v11)"

    def enabled(self, cfg: Any) -> bool:
        return bool(_cfg_get(cfg, "channels", "qq", "enabled"))

    async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
        # Lazy-import to break the import cycle:
        # ``service`` imports ``router`` imports nothing here, but the
        # built-in registry construction can run before ``service`` is
        # imported by the caller. Importing lazily keeps the channel
        # module standalone-importable.
        from corlinman_channels.service import QqChannelParams, run_qq_channel

        qq_cfg = _cfg_get(ctx.config, "channels", "qq")
        if qq_cfg is None:
            raise RuntimeError(
                "qq channel run() called but channels.qq is None"
            )
        params = QqChannelParams(
            config=qq_cfg,
            model=ctx.model,
            chat_service=ctx.chat_service,
            rate_limit_hook=ctx.rate_limit_hook,
            hook_bus=ctx.hook_bus,
        )
        await run_qq_channel(params, cancel)


class TelegramChannel:
    """Telegram long-poll adapter wrapper.

    Forwards :meth:`run` to the Telegram orchestration helper in
    :mod:`corlinman_channels.service`.
    """

    def id(self) -> str:
        return "telegram"

    def display_name(self) -> str:
        return "Telegram"

    def enabled(self, cfg: Any) -> bool:
        return bool(_cfg_get(cfg, "channels", "telegram", "enabled"))

    async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
        from corlinman_channels.service import TelegramChannelParams, run_telegram_channel

        tg_cfg = _cfg_get(ctx.config, "channels", "telegram")
        if tg_cfg is None:
            raise RuntimeError(
                "telegram channel run() called but channels.telegram is None"
            )
        params = TelegramChannelParams(
            config=tg_cfg,
            model=ctx.model,
            chat_service=ctx.chat_service,
        )
        await run_telegram_channel(params, cancel)


class ApnsChannel:
    """APNs (Apple Push Notification service) stub.

    Mirrors the Rust ``ApnsChannel`` stub: reserves the registry slot
    without yet shipping the HTTP/2 + JWT pipeline. :meth:`enabled`
    returns ``False`` until the config grows ``channels.apns``, so
    :func:`spawn_all` never invokes :meth:`run` today. The defensive
    no-op body is kept for the day someone wires the config flag.
    """

    def id(self) -> str:
        return "apns"

    def display_name(self) -> str:
        return "APNs (stub)"

    def enabled(self, cfg: Any) -> bool:
        return False

    async def run(
        self,
        ctx: ChannelContext,
        cancel: asyncio.Event,
    ) -> None:
        """No-op until the APNs adapter actually lands. ``enabled``
        returns ``False`` so this is unreachable in practice."""
        return None
