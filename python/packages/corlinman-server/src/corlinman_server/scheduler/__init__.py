"""``corlinman_server.scheduler`` — Python port of ``corlinman-scheduler``.

Cron-based periodic job runner that shares state with the gateway:

* :func:`parse` / :func:`next_after` / :class:`Schedule` — cron
  expression parsing (5-, 6-, and Rust-native 7-field grammars
  via :mod:`croniter`).
* :class:`SchedulerConfig` / :class:`SchedulerJob` / :class:`JobAction` —
  config-shape dataclasses mirroring the Rust
  ``corlinman_core::config::Scheduler*`` types.
* :class:`JobSpec` / :class:`ActionSpec` — runtime-side specs
  (cron expression already parsed).
* :func:`spawn` / :class:`SchedulerHandle` — start one tick task per
  parseable job; cancel via the shared :class:`asyncio.Event`.
* :func:`dispatch` / :func:`run_subprocess` / :class:`SubprocessOutcome` —
  the per-firing execution path. Public so an admin "fire now"
  endpoint can reuse :func:`dispatch` without driving the tick loop.
* :class:`SchedulerStore` — :mod:`aiosqlite`-backed run-history
  persistence (the Python addition; the Rust crate is in-memory only
  and persists through the hook bus + observers).

Hook events flow through :mod:`corlinman_hooks` (``HookEvent.EngineRunCompleted``
/ ``.EngineRunFailed``), matching the Rust crate's emission shape so
the gateway's evolution observer folds Python-side firings in
transparently.
"""

from __future__ import annotations

from corlinman_server.scheduler.cron import (
    CronParseError,
    Schedule,
    next_after,
    parse,
)
from corlinman_server.scheduler.persistence import (
    SCHEDULER_SCHEMA_SQL,
    RunRecord,
    SchedulerStore,
    SchedulerStoreConnectError,
    SchedulerStoreError,
)
from corlinman_server.scheduler.runner import (
    ActionSpec,
    JobAction,
    JobSpec,
    SchedulerConfig,
    SchedulerHandle,
    SchedulerJob,
    SubprocessOutcome,
    SubprocessOutcomeKind,
    dispatch,
    run_subprocess,
    spawn,
)

__all__ = [
    # cron
    "CronParseError",
    "Schedule",
    "next_after",
    "parse",
    # runner / config
    "ActionSpec",
    "JobAction",
    "JobSpec",
    "SchedulerConfig",
    "SchedulerHandle",
    "SchedulerJob",
    "SubprocessOutcome",
    "SubprocessOutcomeKind",
    "dispatch",
    "run_subprocess",
    "spawn",
    # persistence
    "SCHEDULER_SCHEMA_SQL",
    "RunRecord",
    "SchedulerStore",
    "SchedulerStoreConnectError",
    "SchedulerStoreError",
]
