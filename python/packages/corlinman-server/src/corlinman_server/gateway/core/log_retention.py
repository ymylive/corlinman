"""Periodic cleanup of rotated gateway log files.

Python port of ``rust/crates/corlinman-gateway/src/log_retention.rs``.

The :class:`TimedRotatingFileHandler` Python helpers rotate on a wall-
clock boundary but only keep ``backupCount`` files automatically. The
gateway needs a uniform mtime-based retention policy across both Rust
and Python runtimes, so we re-implement the Rust sweep loop here:

* Wakes once an hour (configurable for tests).
* Scans the directory the file sink writes into.
* Removes any sibling file whose name matches ``<prefix>(.YYYY-MM-DD...)?``
  AND whose ``mtime`` is older than ``retention_days``.
* Bare ``<prefix>`` (the active never-rotation file) is always skipped.

Failure mode: every IO error is warn-and-continue. A broken sweep
won't stop the task; the next tick retries.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import structlog

from corlinman_server.gateway.core.metrics import LOG_FILES_REMOVED

logger = structlog.get_logger(__name__)


#: Sweep cadence in seconds. Set to 1h to mirror the Rust default; tests
#: pass a smaller value via the :class:`LogRetentionTask.interval` kwarg.
SWEEP_INTERVAL_SECONDS: float = 3600.0


def _rotated_file_regex(prefix: str) -> re.Pattern[str]:
    """Build the regex that matches any rotated log file for ``prefix``.

    Covers ``<prefix>``, ``<prefix>.YYYY-MM-DD``,
    ``<prefix>.YYYY-MM-DD-HH``, ``<prefix>.YYYY-MM-DD-HH-mm``. The bare
    ``<prefix>`` (never-rotation active file) also matches so the
    caller can explicitly skip it.
    """

    pattern = rf"^{re.escape(prefix)}(\.\d{{4}}-\d{{2}}-\d{{2}}(-\d{{2}}){{0,2}})?$"
    return re.compile(pattern)


def sweep_once(directory: Path, prefix: str, retention_days: int) -> int:
    """Run one sweep pass. Returns the number of files unlinked.

    ``retention_days == 0`` short-circuits to ``0`` so operators can
    keep "log forever" semantics by zeroing the knob — same shape as
    the Rust contract.
    """

    if retention_days <= 0:
        return 0
    if not directory.is_dir():
        # Missing dir is fine — the file sink may not have written yet.
        return 0

    cutoff_seconds = retention_days * 86_400
    now = time.time()
    matcher = _rotated_file_regex(prefix)

    removed = 0
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if matcher.match(name) is None:
            continue
        # Protect the active never-rotation file: bare prefix.
        if name == prefix:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if now - mtime < cutoff_seconds:
            continue
        try:
            entry.unlink()
        except OSError as err:
            logger.warning(
                "log_retention.unlink_failed",
                file=str(entry),
                error=str(err),
            )
            continue
        removed += 1
        LOG_FILES_REMOVED.labels(reason="age").inc()
    return removed


class LogRetentionTask:
    """Background asyncio task that runs :func:`sweep_once` on a timer.

    Use :meth:`start` to spawn the task and :meth:`stop` to cancel it.
    Constructed once at boot, alongside the file sink, and held on the
    gateway state so shutdown can ``await stop()``.
    """

    def __init__(
        self,
        directory: Path,
        prefix: str,
        retention_days: int,
        interval: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self.prefix = prefix
        self.retention_days = retention_days
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self) -> asyncio.Task[None]:
        """Spawn the background task. Idempotent — returns the existing
        task on subsequent calls."""
        if self._task is not None and not self._task.done():
            return self._task
        loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run(), name="gateway.log_retention")
        return self._task

    async def stop(self) -> None:
        """Cancel the task and await its exit. Safe to call multiple
        times or before :meth:`start`."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is None:
            return
        task = self._task
        self._task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self) -> None:
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass  # interval elapsed; sweep once below
                if self._stop_event.is_set():
                    break
                try:
                    removed = sweep_once(self.directory, self.prefix, self.retention_days)
                except Exception as err:  # pragma: no cover — defensive
                    logger.warning("log_retention.sweep_failed", error=str(err))
                    continue
                if removed > 0:
                    logger.info(
                        "log_retention.swept",
                        removed=removed,
                        directory=str(self.directory),
                        retention_days=self.retention_days,
                    )
        except asyncio.CancelledError:  # graceful shutdown path
            return


__all__ = [
    "LogRetentionTask",
    "SWEEP_INTERVAL_SECONDS",
    "sweep_once",
]
