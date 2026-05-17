"""Service plugin supervisor — spawn, track, respawn.

Python port of ``rust/crates/corlinman-plugins/src/supervisor.rs``. Handles the
process lifecycle for ``plugin_type = "service"`` plugins:

  1. On boot, for every service manifest the gateway holds, spawn the child
     with a per-plugin UDS path exported via ``CORLINMAN_PLUGIN_ADDR``.
  2. After spawn, return the socket path so a gRPC client can dial it.
  3. Run a per-plugin watchdog task that observes child exits and respawns
     with exponential backoff.
  4. After ``MAX_RESTARTS_IN_WINDOW`` crashes inside ``CRASH_LOOP_WINDOW``,
     stop trying and log so the operator investigates.

The supervisor does **not** own any gRPC client cache; the upstream service
runtime is responsible for dialing and re-dialing the UDS path returned by
:meth:`PluginSupervisor.spawn_service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog

from .manifest import PluginManifest

log = structlog.get_logger(__name__)

#: Environment variable name carrying the UDS path to the spawned child.
PLUGIN_ADDR_ENV = "CORLINMAN_PLUGIN_ADDR"

#: Max restart attempts inside :data:`CRASH_LOOP_WINDOW_SECONDS` before we
#: stop trying and emit a persistent error.
MAX_RESTARTS_IN_WINDOW = 3

#: Sliding window (seconds) over which crash counts are evaluated.
CRASH_LOOP_WINDOW_SECONDS = 60.0

#: Backoff schedule (seconds), capped at the final entry for subsequent
#: retries.
BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)


class ServiceRuntimeProtocol(Protocol):
    """Minimal contract the supervisor needs from a service-runtime
    client cache. Decoupled to avoid pulling tonic / grpcio into this
    module: the upstream wiring layer can pass anything that quacks like
    this protocol.
    """

    async def register(self, name: str, socket: Path) -> None: ...
    async def unregister(self, name: str) -> None: ...


@dataclass
class PluginChild:
    """Tracked child process + its UDS path."""

    process: asyncio.subprocess.Process
    socket_path: Path
    last_restart: float = field(default_factory=time.monotonic)


class PluginSupervisorError(Exception):
    """Raised when a spawn fails or the supervisor refuses an operation."""


class PluginSupervisor:
    """Long-lived supervisor holding one child per service plugin."""

    def __init__(self, socket_root: Path | str) -> None:
        self.socket_root = Path(socket_root)
        self._children: dict[str, PluginChild] = {}
        self._watchdogs: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    # -- Accessors --

    def child_count(self) -> int:
        return len(self._children)

    def is_shutting_down(self) -> bool:
        return self._shutdown.is_set()

    # -- Spawn / stop --

    async def spawn_service(self, manifest: PluginManifest) -> Path:
        """Spawn (or respawn) a service plugin and return the UDS path the
        gateway should dial.
        """
        if self._shutdown.is_set():
            raise PluginSupervisorError("supervisor is shutting down")

        try:
            await asyncio.to_thread(
                self.socket_root.mkdir, parents=True, exist_ok=True
            )
        except OSError as err:
            raise PluginSupervisorError(
                f"failed to create socket root {self.socket_root}: {err}"
            ) from err

        socket_path = self.socket_root / f"{manifest.name}.sock"
        # Remove a stale socket from a previous run.
        try:
            if socket_path.exists():
                await asyncio.to_thread(socket_path.unlink)
        except OSError:
            # Non-fatal: the child may overwrite or fail more loudly.
            pass

        env = os.environ.copy()
        env[PLUGIN_ADDR_ENV] = str(socket_path)
        env.update(manifest.entry_point.env)

        try:
            process = await asyncio.create_subprocess_exec(
                manifest.entry_point.command,
                *manifest.entry_point.args,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as err:
            raise PluginSupervisorError(
                f"spawn failed for plugin {manifest.name!r}: {err}"
            ) from err

        async with self._lock:
            self._children[manifest.name] = PluginChild(
                process=process,
                socket_path=socket_path,
                last_restart=time.monotonic(),
            )

        log.info(
            "plugins.supervisor.spawned",
            plugin=manifest.name,
            socket=str(socket_path),
            pid=process.pid,
        )
        return socket_path

    async def stop_service(self, name: str) -> None:
        """Gracefully stop the child for ``name``."""
        async with self._lock:
            tracked = self._children.pop(name, None)
            watchdog = self._watchdogs.pop(name, None)

        if watchdog is not None:
            watchdog.cancel()

        if tracked is None:
            return

        with contextlib.suppress(ProcessLookupError):
            tracked.process.terminate()
        try:
            await asyncio.wait_for(tracked.process.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                tracked.process.kill()
            await tracked.process.wait()

        try:
            if tracked.socket_path.exists():
                await asyncio.to_thread(tracked.socket_path.unlink)
        except OSError:
            pass

        log.info("plugins.supervisor.stopped", plugin=name)

    async def shutdown(self) -> None:
        """Tear down every tracked child; intended for gateway shutdown."""
        self._shutdown.set()
        async with self._lock:
            names = list(self._children.keys())
        for name in names:
            await self.stop_service(name)

    # -- Watchdog --

    def start_watchdog(
        self,
        *,
        name: str,
        manifest: PluginManifest,
        runtime: ServiceRuntimeProtocol | None = None,
        on_respawn: Callable[[str, Path], Awaitable[None]] | None = None,
    ) -> asyncio.Task[None]:
        """Spawn the per-plugin watchdog task.

        Must be called after the initial ``spawn_service`` pair. Either
        ``runtime`` (with ``register`` / ``unregister`` methods) or
        ``on_respawn`` (callable) may be passed to react to respawns; both
        are optional for callers that only want crash-loop bookkeeping.
        """
        task = asyncio.create_task(
            self._watchdog_loop(name, manifest, runtime, on_respawn)
        )
        self._watchdogs[name] = task
        return task

    async def _watchdog_loop(
        self,
        name: str,
        manifest: PluginManifest,
        runtime: ServiceRuntimeProtocol | None,
        on_respawn: Callable[[str, Path], Awaitable[None]] | None,
    ) -> None:
        crash_times: list[float] = []
        attempt = 0

        try:
            while not self._shutdown.is_set():
                async with self._lock:
                    tracked = self._children.get(name)
                if tracked is None:
                    log.debug(
                        "plugins.supervisor.watchdog.no_child",
                        plugin=name,
                    )
                    return

                # Wait for the process to exit OR for shutdown.
                wait_task = asyncio.create_task(tracked.process.wait())
                shutdown_task = asyncio.create_task(self._shutdown.wait())
                done, _ = await asyncio.wait(
                    {wait_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if shutdown_task in done and wait_task not in done:
                    wait_task.cancel()
                    return

                shutdown_task.cancel()
                exit_status = wait_task.result()

                if runtime is not None:
                    try:
                        await runtime.unregister(name)
                    except Exception:  # pragma: no cover — defensive
                        log.warning(
                            "plugins.supervisor.unregister_failed",
                            plugin=name,
                        )

                try:
                    if tracked.socket_path.exists():
                        await asyncio.to_thread(tracked.socket_path.unlink)
                except OSError:
                    pass

                async with self._lock:
                    self._children.pop(name, None)

                now = time.monotonic()
                crash_times = [
                    t for t in crash_times if (now - t) <= CRASH_LOOP_WINDOW_SECONDS
                ]
                crash_times.append(now)

                log.warning(
                    "plugins.supervisor.child_exited",
                    plugin=name,
                    exit_status=exit_status,
                    crashes_in_window=len(crash_times),
                )

                if len(crash_times) > MAX_RESTARTS_IN_WINDOW:
                    log.error(
                        "plugins.supervisor.crash_loop_giving_up",
                        plugin=name,
                        window_secs=CRASH_LOOP_WINDOW_SECONDS,
                    )
                    return

                backoff = BACKOFF_SCHEDULE_SECONDS[
                    min(attempt, len(BACKOFF_SCHEDULE_SECONDS) - 1)
                ]
                attempt += 1

                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=backoff
                    )
                    return  # shutdown fired during backoff
                except TimeoutError:
                    pass  # backoff completed normally

                try:
                    socket_path = await self.spawn_service(manifest)
                except PluginSupervisorError as err:
                    log.error(
                        "plugins.supervisor.respawn_failed",
                        plugin=name,
                        error=str(err),
                    )
                    continue

                if runtime is not None:
                    try:
                        await runtime.register(name, socket_path)
                    except Exception as err:  # pragma: no cover — defensive
                        log.error(
                            "plugins.supervisor.reregister_failed",
                            plugin=name,
                            error=str(err),
                        )
                        # Force the child to restart on the next iteration.
                        async with self._lock:
                            entry = self._children.get(name)
                        if entry is not None:
                            with contextlib.suppress(ProcessLookupError):
                                entry.process.kill()
                        continue

                if on_respawn is not None:
                    try:
                        await on_respawn(name, socket_path)
                    except Exception:  # pragma: no cover — defensive
                        log.warning(
                            "plugins.supervisor.on_respawn_failed",
                            plugin=name,
                        )

                log.info("plugins.supervisor.respawned", plugin=name)
        except asyncio.CancelledError:
            return


__all__ = [
    "BACKOFF_SCHEDULE_SECONDS",
    "CRASH_LOOP_WINDOW_SECONDS",
    "MAX_RESTARTS_IN_WINDOW",
    "PLUGIN_ADDR_ENV",
    "PluginChild",
    "PluginSupervisor",
    "PluginSupervisorError",
    "ServiceRuntimeProtocol",
]
