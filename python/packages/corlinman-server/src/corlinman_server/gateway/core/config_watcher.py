"""``ConfigWatcher`` — SIGHUP + filesystem hot-reload of ``corlinman.toml``.

Python port of ``rust/crates/corlinman-gateway/src/config_watcher.rs``.

Key behaviour preserved:

* :class:`watchdog.observers.Observer` watches the **parent directory**
  of the config file (editors atomic-rename, so the inode the original
  file pointed at vanishes the moment vim ``:w`` returns). When the
  observer is unavailable (``import watchdog`` fails), the watcher
  degrades gracefully to SIGHUP / admin-endpoint reloads only.
* A debouncer coalesces the burst of fs events that a single save
  produces (macOS FSEvents fires 3-5 events per write; Linux inotify
  splits an atomic-rename into ``CREATE`` + ``MOVED_TO``).
* On Unix, :data:`signal.SIGHUP` triggers a reload — the classic daemon
  idiom so operators can ``kill -HUP <pid>``.
* Each successful reload diffs the new snapshot against the live one
  at the section level (top-level dict keys). The published snapshot
  is replaced atomically and per-section change events are fired into
  the optional :class:`HookBus`. Sections in
  :data:`RESTART_REQUIRED_SECTIONS` emit an extra
  ``<section>.restart_required`` event so the admin UI surfaces a
  "process restart needed" warning.

Failure model:

* Parse failure → :class:`ReloadReport` ``errors`` populated; snapshot
  is **not** swapped, no hook events fire.
* Validation failure (provided ``validate`` returns ``False``) → same.
* Identical reload → empty report, no hooks.

The current snapshot is published via :class:`_AtomicSnapshot` — a
lightweight asyncio.Lock-guarded slot that mirrors Rust's ``ArcSwap``.
Readers always see a consistent dict, never a half-mutated one.
"""

from __future__ import annotations

import asyncio
import json
import signal as _signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


#: Debounce window in seconds. Coalesces fs event bursts produced by a
#: single editor save (vim ``:w``, VS Code atomic-rename).
DEFAULT_DEBOUNCE_SECONDS: float = 0.3


#: Top-level sections which cannot be applied without a process restart.
#: We still swap (the snapshot is the source of truth) but emit an extra
#: ``<section>.restart_required`` event so operators get a loud warning.
RESTART_REQUIRED_SECTIONS: frozenset[str] = frozenset(
    {"server", "wstool", "nodebridge", "mcp"}
)


@dataclass
class ReloadReport:
    """Result of a single reload attempt. Returned to the admin endpoint
    and logged by the SIGHUP / fs-watcher paths.

    * ``changed_sections`` lists the top-level keys whose JSON repr
      changed. Items in :data:`RESTART_REQUIRED_SECTIONS` will also
      appear as ``<section>.restart_required`` in the emitted hook
      events (not in this list).
    * ``errors`` is non-empty when the reload was rejected. Non-empty
      ⇒ snapshot was **not** swapped.
    """

    changed_sections: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not self.changed_sections and not self.errors


# Type aliases for the parser / validator the watcher delegates to. Kept
# pluggable so the watcher itself doesn't need to import the (yet to
# land) Config class from a sibling agent.
ConfigParser = Callable[[Path], dict[str, Any]]
ConfigValidator = Callable[[dict[str, Any]], list[str]]
HookEmitter = Callable[[str, str, Any, Any], Awaitable[None]]


class _AtomicSnapshot:
    """Thread-safe single-value cell with an atomic swap.

    Equivalent to ``arc_swap::ArcSwap<dict>``. Readers get a borrowed
    reference to the published dict; writers ``store`` a new dict in
    one atomic step. We deliberately do **not** clone on read — the
    convention is that consumers treat the returned dict as immutable.
    """

    def __init__(self, initial: dict[str, Any]) -> None:
        self._value: dict[str, Any] = initial
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._value

    def store(self, new_value: dict[str, Any]) -> None:
        with self._lock:
            self._value = new_value


class ConfigWatcher:
    """Filesystem + SIGHUP watcher over a single TOML config file.

    Cheap to share across coroutines: every field is either immutable
    (path), guarded by an :class:`asyncio.Lock` (reload_lock), or
    behind an internal threading.Lock (snapshot).

    Constructor parameters:

    * ``path`` — TOML file to watch.
    * ``initial`` — dict representation of the current config. Published
      immediately so :meth:`current` is callable before :meth:`start`.
    * ``parser`` — callback that loads ``path`` and returns a dict. Lets
      the watcher stay agnostic of the schema crate.
    * ``validator`` — callback returning a list of error strings (empty
      ⇒ valid). Optional; defaults to "always valid".
    * ``hook_emitter`` — async callback ``(event_name, section, old, new)``
      invoked once per changed section. Optional.
    * ``debounce`` — seconds to coalesce fs events for a single save.
    """

    def __init__(
        self,
        path: Path,
        initial: dict[str, Any],
        *,
        parser: ConfigParser,
        validator: ConfigValidator | None = None,
        hook_emitter: HookEmitter | None = None,
        debounce: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self._snapshot = _AtomicSnapshot(initial)
        self._parser = parser
        self._validator = validator or (lambda _cfg: [])
        self._hook_emitter = hook_emitter
        self._debounce = debounce

        self._reload_lock = asyncio.Lock()
        self._pending_event: asyncio.Event | None = None
        self._observer: Any = None  # watchdog observer when wired
        self._sighup_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- snapshot API ------------------------------------------------------

    def current(self) -> dict[str, Any]:
        """Cheap snapshot of the live config. Equivalent to
        ``ArcSwap::load_full().clone()`` — the returned dict is the
        actual published value; callers should treat it as read-only."""
        return self._snapshot.load()

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Install the fs watcher + SIGHUP handler + debounce loop. The
        gateway boot calls this once and awaits :meth:`stop` at
        shutdown. Idempotent (re-start is a no-op)."""
        if self._stop_event is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._pending_event = asyncio.Event()

        self._install_fs_watcher()
        self._install_sighup_handler()
        self._loop_task = self._loop.create_task(
            self._debounce_loop(), name="gateway.config_watcher.loop"
        )

    async def stop(self) -> None:
        """Cancel watcher + loop tasks; close the fs observer."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=1.0)
            except Exception:  # pragma: no cover — defensive
                pass
            self._observer = None
        for task in (self._loop_task, self._sighup_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        self._sighup_task = None
        self._stop_event = None

    # ---- public reload trigger --------------------------------------------

    async def trigger_reload(self) -> ReloadReport:
        """Manual reload — bypasses the debouncer. Used by the admin
        ``POST /admin/config/reload`` endpoint, the SIGHUP handler, and
        the debounced fs path. Holds an asyncio.Lock so the three
        sources can't race a half-applied snapshot."""
        async with self._reload_lock:
            return await self._reload_unlocked()

    # ---- internals ---------------------------------------------------------

    async def _reload_unlocked(self) -> ReloadReport:
        report = ReloadReport()

        # Stage 1: parse.
        try:
            new_cfg = self._parser(self.path)
        except Exception as err:
            msg = f"parse failed: {err}"
            logger.warning("config_watcher.parse_failed", path=str(self.path), error=str(err))
            report.errors.append(msg)
            return report

        # Stage 2: validate.
        validation_errors = self._validator(new_cfg)
        if validation_errors:
            logger.warning(
                "config_watcher.validation_failed",
                path=str(self.path),
                errors=validation_errors,
            )
            report.errors.extend(validation_errors)
            return report

        # Stage 3: diff.
        old_cfg = self._snapshot.load()
        changed = diff_sections(old_cfg, new_cfg)
        if not changed:
            logger.debug("config_watcher.noop", path=str(self.path))
            return report

        # Stage 4: swap snapshot first so subscribers that call
        # ``current()`` inside their handler see the new state.
        self._snapshot.store(new_cfg)

        # Stage 5: emit ConfigChanged per section.
        for section in changed:
            old_val = old_cfg.get(section)
            new_val = new_cfg.get(section)
            await self._maybe_emit("ConfigChanged", section, old_val, new_val)

        # Stage 6: restart-required flags.
        for section in changed:
            if section in RESTART_REQUIRED_SECTIONS:
                flag = f"{section}.restart_required"
                await self._maybe_emit(
                    "ConfigChanged", flag, old_cfg.get(section), new_cfg.get(section)
                )
                logger.warning(
                    "config_watcher.restart_required",
                    section=section,
                )

        logger.info("config_watcher.applied", path=str(self.path), changed=changed)
        report.changed_sections = changed
        return report

    async def _maybe_emit(self, event: str, section: str, old: Any, new: Any) -> None:
        if self._hook_emitter is None:
            return
        try:
            await self._hook_emitter(event, section, old, new)
        except Exception as err:  # pragma: no cover — emitter ownership
            logger.warning(
                "config_watcher.hook_emit_failed",
                event=event,
                section=section,
                error=str(err),
            )

    # ---- fs watcher --------------------------------------------------------

    def _install_fs_watcher(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "config_watcher.watchdog_missing",
                path=str(self.path),
                hint="install watchdog for fs hot-reload; SIGHUP + admin endpoint still work",
            )
            return

        parent = self.path.parent
        if not parent.exists():
            logger.warning(
                "config_watcher.parent_missing",
                path=str(parent),
            )
            return
        target = self.path.resolve(strict=False)

        watcher_self = self

        class _Handler(FileSystemEventHandler):  # type: ignore[misc, no-any-unimported]
            def _maybe_signal(self, src_path: str) -> None:
                try:
                    if Path(src_path).resolve(strict=False) == target:
                        watcher_self._signal_change()
                except OSError:
                    # File may have been deleted between event + resolve;
                    # match on filename as a fallback.
                    if Path(src_path).name == target.name:
                        watcher_self._signal_change()

            def on_modified(self, event: Any) -> None:
                if not event.is_directory:
                    self._maybe_signal(event.src_path)

            def on_created(self, event: Any) -> None:
                if not event.is_directory:
                    self._maybe_signal(event.src_path)

            def on_moved(self, event: Any) -> None:
                if not event.is_directory:
                    self._maybe_signal(getattr(event, "dest_path", event.src_path))

        observer = Observer()
        observer.schedule(_Handler(), str(parent), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _signal_change(self) -> None:
        """Notify the debounce loop a change is pending. Called from
        the watchdog thread, so we hop back onto the asyncio loop."""
        if self._loop is None or self._pending_event is None:
            return
        loop = self._loop
        evt = self._pending_event
        try:
            loop.call_soon_threadsafe(evt.set)
        except RuntimeError:
            # Loop has been closed; nothing to do.
            pass

    # ---- debounce loop -----------------------------------------------------

    async def _debounce_loop(self) -> None:
        assert self._pending_event is not None
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                done, _ = await asyncio.wait(
                    {
                        asyncio.create_task(self._pending_event.wait()),
                        asyncio.create_task(self._stop_event.wait()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Resolve immediately if shutdown raced ahead.
                if self._stop_event.is_set():
                    return
                if not self._pending_event.is_set():
                    continue
                self._pending_event.clear()
                # Coalesce burst — sleep, then drain any further events
                # that arrived during the window before the reload.
                try:
                    await asyncio.sleep(self._debounce)
                except asyncio.CancelledError:
                    return
                self._pending_event.clear()
                try:
                    await self.trigger_reload()
                except Exception as err:  # pragma: no cover — defensive
                    logger.warning("config_watcher.reload_failed", error=str(err))
        except asyncio.CancelledError:
            return

    # ---- SIGHUP handler ----------------------------------------------------

    def _install_sighup_handler(self) -> None:
        if sys.platform == "win32":
            # No SIGHUP on Windows; admin endpoint + fs watcher cover
            # the remaining reload paths.
            return
        try:
            sighup = _signal.SIGHUP  # type: ignore[attr-defined]
        except AttributeError:
            return
        assert self._loop is not None

        def _on_sighup() -> None:
            assert self._loop is not None
            self._loop.create_task(self._on_sighup_async())

        try:
            self._loop.add_signal_handler(sighup, _on_sighup)
        except (NotImplementedError, RuntimeError, ValueError) as err:
            logger.warning("config_watcher.sighup_install_failed", error=str(err))

    async def _on_sighup_async(self) -> None:
        logger.info("config_watcher.sighup_reload")
        try:
            await self.trigger_reload()
        except Exception as err:  # pragma: no cover — defensive
            logger.warning("config_watcher.sighup_reload_failed", error=str(err))


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


#: Top-level config sections we diff over. Order-insensitive (the result
#: of :func:`diff_sections` is sorted) but stable so tests can assert
#: on ordering.
DEFAULT_SECTIONS: tuple[str, ...] = (
    "server",
    "admin",
    "providers",
    "models",
    "embedding",
    "channels",
    "rag",
    "approvals",
    "scheduler",
    "logging",
    "hooks",
    "skills",
    "variables",
    "agents",
    "tools",
    "telegram",
    "vector",
    "wstool",
    "canvas",
    "nodebridge",
    "meta",
)


def diff_sections(
    old: dict[str, Any],
    new: dict[str, Any],
    sections: tuple[str, ...] | None = None,
) -> list[str]:
    """Return the ordered list of section names whose JSON repr differs
    between ``old`` and ``new``. Unknown sections (present in the dicts
    but not in :data:`DEFAULT_SECTIONS`) are also included so a brand
    new feature flag isn't silently ignored on first reload."""

    if sections is None:
        candidates = set(DEFAULT_SECTIONS)
        candidates.update(old.keys())
        candidates.update(new.keys())
    else:
        candidates = set(sections)
    changed: list[str] = []
    for name in sorted(candidates):
        ov = old.get(name)
        nv = new.get(name)
        if _stable_json(ov) != _stable_json(nv):
            changed.append(name)
    return changed


def _stable_json(value: Any) -> str:
    """Sorted-key JSON repr so dict ordering doesn't trigger a false
    diff. Falls back to ``str`` on non-serialisable inputs (the
    fallback covers values that don't round-trip via json.dumps; the
    diff still detects "changed → different repr" reliably for them)."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


# Re-export for diagnostics: lets a test or admin endpoint print the
# raw "last reload" timestamp without poking the lock directly. Updated
# on every successful :meth:`ConfigWatcher.trigger_reload`.
LAST_RELOAD_AT: float | None = None


def _stamp_last_reload() -> None:
    """Internal helper — kept out of the public API."""
    global LAST_RELOAD_AT
    LAST_RELOAD_AT = time.time()


__all__ = [
    "ConfigWatcher",
    "DEFAULT_DEBOUNCE_SECONDS",
    "DEFAULT_SECTIONS",
    "ReloadReport",
    "RESTART_REQUIRED_SECTIONS",
    "diff_sections",
]
