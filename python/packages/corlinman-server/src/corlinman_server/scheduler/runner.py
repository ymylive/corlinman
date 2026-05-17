"""Async tick loop + per-job dispatcher + subprocess wrapper.

Python port of:

* ``rust/crates/corlinman-scheduler/src/jobs.rs`` — config-shape
  dataclasses + ``JobSpec`` / ``ActionSpec`` runtime types.
* ``rust/crates/corlinman-scheduler/src/runtime.rs`` — :func:`spawn`,
  :class:`SchedulerHandle`, :func:`dispatch`, the per-job tick loop.
* ``rust/crates/corlinman-scheduler/src/subprocess.rs`` — the
  :class:`SubprocessOutcome` enum + :func:`run_subprocess` helper.

The three Rust files collapse into one Python module because the
brief explicitly asks for a 3-module decomposition (``cron.py``,
``runner.py``, ``persistence.py``) — keeping subprocess + runtime
together here matches the typical Python "one module per
responsibility" rule while still keeping each section under a
screenful.

Hook events flow through :mod:`corlinman_hooks` (the workspace's
Python port of ``corlinman-hooks``). The Rust crate emits
``HookEvent::EngineRunCompleted`` / ``::EngineRunFailed`` on the bus
shared with the gateway; the Python port emits the exact same two
variants (``HookEvent.EngineRunCompleted`` / ``.EngineRunFailed``)
through the same bus type so the gateway's evolution-observer code
folds the outcomes in transparently.

Cancellation flows through an :class:`asyncio.Event` (the Python
analogue of ``tokio_util::sync::CancellationToken``). The spawn
returns a handle whose :meth:`SchedulerHandle.join_all` waits for
every per-job task to exit; the gateway shutdown path flips the
cancel event and then awaits ``join_all``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from corlinman_hooks import HookBus, HookEvent

from corlinman_server.scheduler.cron import Schedule, next_after, parse

if TYPE_CHECKING:
    # Typing-only: a hook bus type alias; we import for symmetry with
    # the Rust ``Arc<HookBus>`` signature.
    pass

_logger = logging.getLogger("corlinman_server.scheduler")


# ---------------------------------------------------------------------------
# Config-shape dataclasses (mirror corlinman_core::config::Scheduler*).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobAction:
    """Discriminated union of the three job actions.

    Mirrors the Rust ``JobAction`` enum (``RunAgent``, ``RunTool``,
    ``Subprocess``). Python's tagged-union idiom is a frozen dataclass
    with a ``kind`` discriminant plus the per-kind fields nullable;
    the constructors :meth:`subprocess`, :meth:`run_agent`,
    :meth:`run_tool` keep call sites readable.

    Only :attr:`kind` ``"subprocess"`` is end-to-end (matches the Rust
    Wave 2-B reality); the other two surface as ``unsupported_action``
    failures on the bus when fired.
    """

    kind: str
    # subprocess fields
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout_secs: int = 600  # default mirrors `default_subprocess_timeout_secs`
    working_dir: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    # run_agent field
    prompt: str | None = None
    # run_tool fields
    plugin: str | None = None
    tool: str | None = None
    tool_args: object = None  # serde_json::Value analog — opaque

    @classmethod
    def subprocess(
        cls,
        command: str,
        args: Sequence[str] = (),
        timeout_secs: int = 600,
        working_dir: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> JobAction:
        """Build a ``Subprocess`` action.

        ``timeout_secs`` defaults to 600 (10 min) to match the Rust
        ``default_subprocess_timeout_secs`` serde default. ``env``
        defaults to an empty mapping; entries are merged over the
        inherited environment at spawn time."""
        return cls(
            kind="subprocess",
            command=command,
            args=tuple(args),
            timeout_secs=timeout_secs,
            working_dir=(Path(working_dir) if working_dir is not None else None),
            env=dict(env) if env else {},
        )

    @classmethod
    def run_agent(cls, prompt: str) -> JobAction:
        """Build a ``RunAgent`` action (not yet implemented at dispatch)."""
        return cls(kind="run_agent", prompt=prompt)

    @classmethod
    def run_tool(cls, plugin: str, tool: str, args: object = None) -> JobAction:
        """Build a ``RunTool`` action (not yet implemented at dispatch)."""
        return cls(kind="run_tool", plugin=plugin, tool=tool, tool_args=args)


@dataclass(frozen=True)
class SchedulerJob:
    """One ``[[scheduler.jobs]]`` table entry.

    Mirrors the Rust ``SchedulerJob`` struct. ``timezone`` is accepted
    for parity with the TOML schema; the Python port treats it as
    advisory (croniter's tz handling differs from the Rust ``cron``
    crate's, so we keep everything in UTC and surface tz support in a
    follow-up wave if a user actually files a bug).
    """

    name: str
    cron: str
    action: JobAction
    timezone: str | None = None


@dataclass(frozen=True)
class SchedulerConfig:
    """Whole ``[scheduler]`` config block.

    Mirrors the Rust ``SchedulerConfig``. ``jobs`` defaults to empty
    so a config with no scheduler block produces a no-op scheduler.
    """

    jobs: tuple[SchedulerJob, ...] = ()


# ---------------------------------------------------------------------------
# Runtime-side specs (cron expression already parsed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """Runtime-side action carrier. Same kinds as :class:`JobAction`
    but post-validation: no Optional-everywhere shape, fields are
    typed per kind via the discriminant. We keep a single dataclass
    (rather than three subclasses) because the dispatch table reads
    cleaner as one ``if/elif`` chain than as a class hierarchy."""

    kind: str  # "subprocess" | "run_agent" | "run_tool"
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout_secs: int = 600
    working_dir: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    prompt: str | None = None
    plugin: str | None = None
    tool: str | None = None
    tool_args: object = None


@dataclass(frozen=True)
class JobSpec:
    """A scheduler job after validation. Holds the parsed cron
    :class:`Schedule` so the tick loop never re-parses the expression."""

    name: str
    cron: Schedule
    action: ActionSpec

    @classmethod
    def from_config(cls, job: SchedulerJob) -> JobSpec | None:
        """Mirror of Rust ``JobSpec::from_config``.

        Returns ``None`` (with a ``warning`` log) when the cron
        expression fails to parse — the caller should drop the job
        rather than abort scheduler startup, exactly as the Rust crate
        does. Tests assert both branches.
        """
        try:
            schedule = parse(job.cron)
        except Exception as exc:  # noqa: BLE001 - mirror Rust's catch-all
            _logger.warning(
                "scheduler: dropping job with unparseable cron",
                extra={"job": job.name, "cron": job.cron, "error": str(exc)},
            )
            return None
        action = ActionSpec(
            kind=job.action.kind,
            command=job.action.command,
            args=job.action.args,
            timeout_secs=job.action.timeout_secs,
            working_dir=job.action.working_dir,
            env=job.action.env,
            prompt=job.action.prompt,
            plugin=job.action.plugin,
            tool=job.action.tool,
            tool_args=job.action.tool_args,
        )
        return cls(name=job.name, cron=schedule, action=action)


# ---------------------------------------------------------------------------
# Subprocess execution (mirrors src/subprocess.rs).
# ---------------------------------------------------------------------------


class SubprocessOutcomeKind(str, Enum):
    """Discriminant for :class:`SubprocessOutcome`. Matches the Rust
    enum variant names 1:1 so the wire/log surfaces look the same."""

    SUCCESS = "success"
    NON_ZERO_EXIT = "non_zero_exit"
    TIMEOUT = "timeout"
    SPAWN_FAILED = "spawn_failed"


@dataclass(frozen=True)
class SubprocessOutcome:
    """Outcome of one subprocess firing. Enum-shaped so callers can
    pattern-match on :attr:`kind` and pick the right ``error_kind``
    string for ``EngineRunFailed``.

    Field meanings per ``kind``:

    * ``SUCCESS``: ``duration_secs`` set.
    * ``NON_ZERO_EXIT``: ``duration_secs`` + optional ``exit_code``.
    * ``TIMEOUT``: ``duration_secs`` is the timeout we hit.
    * ``SPAWN_FAILED``: ``error`` is the OS error message.
    """

    kind: SubprocessOutcomeKind
    duration_secs: float = 0.0
    exit_code: int | None = None
    error: str | None = None


async def _forward_stream(stream: asyncio.StreamReader, job: str, run_id: str, level: int, label: str) -> None:
    """Forward a piped child stdout/stderr line-by-line into logging.

    Mirrors the Rust ``BufReader::lines`` + ``tracing::{info,warn}!``
    loop. Each line carries the job + run_id + stream label so multiple
    concurrent jobs are distinguishable in logs.
    """
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.CancelledError, ValueError):
            # ValueError can be raised when the pipe is closed mid-read
            # on some Python versions; treat as end-of-stream.
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            # Skip empty lines so the log isn't padded with blanks.
            continue
        _logger.log(
            level,
            "scheduler: subprocess %s: %s",
            label,
            line,
            extra={"job": job, "run_id": run_id, "stream": label},
        )


async def run_subprocess(
    job: str,
    run_id: str,
    command: str,
    args: Sequence[str],
    timeout_secs: int,
    working_dir: Path | None,
    env: Mapping[str, str],
) -> SubprocessOutcome:
    """Spawn ``command args`` and wait up to ``timeout_secs`` for it.

    Behaviour matches the Rust :func:`run_subprocess`:

    * stdout/stderr piped + forwarded to :mod:`logging` line-by-line
      (stdout at INFO, stderr at WARNING).
    * :func:`asyncio.wait_for` wraps the child wait. On expiry we send
      SIGKILL (``Process.kill()``) and return :class:`SubprocessOutcome`
      ``TIMEOUT``. The strong kill is deliberate — a graceful SIGTERM
      could let a wedged engine outlive the schedule's next firing.
    * ``Command::spawn``-equivalent failures (binary not on PATH,
      missing working dir, etc.) surface as ``SPAWN_FAILED`` so the
      caller emits ``EngineRunFailed { error_kind: "spawn_failed" }``
      without the gateway crashing.

    ``env`` is merged over the inherited environment (parity with the
    Rust ``Command::env`` behaviour — only the explicit entries are
    overridden; PATH/etc. inherit). The Rust side uses
    ``BTreeMap<String, String>`` so iteration is deterministic; Python
    dicts preserve insertion order which is good enough for the same
    "tests see a stable PATH" effect.
    """
    started = time.monotonic()

    merged_env = dict(os.environ)
    for k, v in env.items():
        merged_env[k] = v

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=(str(working_dir) if working_dir is not None else None),
            env=merged_env,
        )
    except (OSError, FileNotFoundError) as exc:
        # `FileNotFoundError` is the common "missing binary" case; the
        # base `OSError` catches permission denied, missing working
        # dir, etc. Wrap into the SPAWN_FAILED variant.
        _logger.error(
            "scheduler: subprocess spawn failed",
            extra={"job": job, "run_id": run_id, "command": command, "error": str(exc)},
        )
        return SubprocessOutcome(
            kind=SubprocessOutcomeKind.SPAWN_FAILED,
            error=str(exc),
            duration_secs=time.monotonic() - started,
        )

    # Spawn the per-stream forwarders. They exit cleanly when the
    # child closes its end of the pipe; we keep references so we can
    # `await` them after the child exits (avoids racing on a pipe
    # close-vs-read).
    fwd_tasks: list[asyncio.Task[None]] = []
    if proc.stdout is not None:
        fwd_tasks.append(
            asyncio.create_task(
                _forward_stream(proc.stdout, job, run_id, logging.INFO, "stdout"),
                name=f"scheduler-fwd-stdout-{run_id}",
            )
        )
    if proc.stderr is not None:
        fwd_tasks.append(
            asyncio.create_task(
                _forward_stream(proc.stderr, job, run_id, logging.WARNING, "stderr"),
                name=f"scheduler-fwd-stderr-{run_id}",
            )
        )

    timeout = max(1, int(timeout_secs))
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _logger.error(
            "scheduler: subprocess timed out; sending SIGKILL",
            extra={"job": job, "run_id": run_id, "timeout_secs": timeout},
        )
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        # Reap so the OS releases the slot. Bound the post-kill wait
        # so a wedged kernel can't park us forever.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        # Drain the forwarders before returning so log lines from
        # straggling stdout buffers aren't reordered after the
        # outcome log.
        for t in fwd_tasks:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        return SubprocessOutcome(kind=SubprocessOutcomeKind.TIMEOUT, duration_secs=float(timeout))

    elapsed = time.monotonic() - started
    # Wait for the forwarders to drain. They exit on EOF, which the
    # `proc.wait()` above is the natural barrier for.
    for t in fwd_tasks:
        with contextlib.suppress(BaseException):
            await t

    if rc == 0:
        return SubprocessOutcome(kind=SubprocessOutcomeKind.SUCCESS, duration_secs=elapsed)
    return SubprocessOutcome(
        kind=SubprocessOutcomeKind.NON_ZERO_EXIT,
        duration_secs=elapsed,
        exit_code=int(rc) if rc is not None else None,
    )


# ---------------------------------------------------------------------------
# Dispatcher (mirrors runtime::dispatch + emit_outcome).
# ---------------------------------------------------------------------------


async def dispatch(spec: JobSpec, bus: HookBus) -> None:
    """Run a single firing of ``spec`` and emit the matching hook event.

    Public so an admin "fire now" endpoint can reuse it later (the Rust
    crate exposes the same surface for the same reason); the per-job
    tick loop calls this on every wake.
    """
    run_id = uuid.uuid4().hex
    if spec.action.kind == "subprocess":
        _logger.info(
            "scheduler: subprocess job firing",
            extra={"job": spec.name, "run_id": run_id, "command": spec.action.command},
        )
        assert spec.action.command is not None  # noqa: S101 - shape-asserted by ActionSpec
        outcome = await run_subprocess(
            spec.name,
            run_id,
            spec.action.command,
            spec.action.args,
            spec.action.timeout_secs,
            spec.action.working_dir,
            spec.action.env,
        )
        await _emit_outcome(bus, spec.name, run_id, outcome)
        return

    # run_agent / run_tool: not yet implemented end-to-end. Surface as
    # an EngineRunFailed with error_kind "unsupported_action" so the
    # gateway's evolution observer sees the failure on the bus rather
    # than a silent drop.
    _logger.warning(
        "scheduler: action kind not yet implemented; skipping fire",
        extra={"job": spec.name, "run_id": run_id, "kind": spec.action.kind},
    )
    await _emit_failed(bus, run_id, "unsupported_action", None)


async def _emit_outcome(bus: HookBus, job: str, run_id: str, outcome: SubprocessOutcome) -> None:
    """Translate a :class:`SubprocessOutcome` into the right hook event.

    Best-effort: hook-bus emit failures are caught and logged but not
    propagated — mirrors the gateway's "hooks never crash the caller"
    stance and the Rust ``if let Err(...) = bus.emit(...)`` pattern.
    """
    duration_ms = int(outcome.duration_secs * 1000)
    if outcome.kind is SubprocessOutcomeKind.SUCCESS:
        _logger.info(
            "scheduler: subprocess job completed",
            extra={"job": job, "run_id": run_id, "duration_ms": duration_ms},
        )
        # Wave 2-B doesn't parse engine stdout for a proposals count
        # yet; report 0 so the schema is honoured (Rust does the same).
        event = HookEvent.EngineRunCompleted(
            run_id=run_id, proposals_generated=0, duration_ms=duration_ms
        )
    elif outcome.kind is SubprocessOutcomeKind.NON_ZERO_EXIT:
        _logger.error(
            "scheduler: subprocess job exited non-zero",
            extra={
                "job": job,
                "run_id": run_id,
                "exit_code": outcome.exit_code,
                "duration_ms": duration_ms,
            },
        )
        event = HookEvent.EngineRunFailed(
            run_id=run_id, error_kind="exit_code", exit_code=outcome.exit_code
        )
    elif outcome.kind is SubprocessOutcomeKind.TIMEOUT:
        _logger.error(
            "scheduler: subprocess job timed out",
            extra={"job": job, "run_id": run_id, "duration_ms": duration_ms},
        )
        event = HookEvent.EngineRunFailed(run_id=run_id, error_kind="timeout", exit_code=None)
    elif outcome.kind is SubprocessOutcomeKind.SPAWN_FAILED:
        _logger.error(
            "scheduler: subprocess job spawn failed",
            extra={"job": job, "run_id": run_id, "error": outcome.error},
        )
        event = HookEvent.EngineRunFailed(
            run_id=run_id, error_kind="spawn_failed", exit_code=None
        )
    else:  # pragma: no cover - exhaustive over the enum
        raise AssertionError(f"unknown SubprocessOutcomeKind: {outcome.kind}")

    try:
        await bus.emit(event)
    except Exception as exc:  # noqa: BLE001 - any emit failure is non-fatal
        _logger.warning(
            "scheduler: hook emit failed",
            extra={"job": job, "run_id": run_id, "error": str(exc)},
        )


async def _emit_failed(bus: HookBus, run_id: str, error_kind: str, exit_code: int | None) -> None:
    """Helper for the unsupported-action branch (no outcome to wrap)."""
    try:
        await bus.emit(
            HookEvent.EngineRunFailed(run_id=run_id, error_kind=error_kind, exit_code=exit_code)
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "scheduler: hook emit failed", extra={"run_id": run_id, "error": str(exc)}
        )


# ---------------------------------------------------------------------------
# Per-job tick loop + spawn (mirrors runtime::run_job_loop + ::spawn).
# ---------------------------------------------------------------------------


class SchedulerHandle:
    """Handle to a running scheduler.

    Mirrors the Rust ``SchedulerHandle`` — holds the per-job
    :class:`asyncio.Task` references so the gateway shutdown path can
    await them after flipping the cancel event.
    """

    __slots__ = ("_cancel", "_tasks")

    def __init__(self, tasks: list[asyncio.Task[None]], cancel: asyncio.Event) -> None:
        self._tasks = tasks
        self._cancel = cancel

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """The per-job tick tasks. Read-only for inspection; tests use
        this to assert "spawn returned N tasks for N parseable jobs"."""
        return list(self._tasks)

    @property
    def cancel_event(self) -> asyncio.Event:
        """The cancellation flag shared with all tick loops. Flipping
        this stops every per-job loop at its next select-point."""
        return self._cancel

    def cancel(self) -> None:
        """Flip the cancel event. Convenience for tests; the gateway
        shutdown path flips its own event (passed into :func:`spawn`)."""
        self._cancel.set()

    async def join_all(self) -> None:
        """Drain every task, swallowing per-task errors.

        Mirrors the Rust ``SchedulerHandle::join_all`` — the gateway
        shutdown path calls this; tests typically inspect tasks
        directly via :attr:`tasks`.
        """
        if not self._tasks:
            return
        # ``gather(return_exceptions=True)`` so one task's CancelledError
        # doesn't mask another's normal exit.
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def _sleep_until(deadline: float, cancel: asyncio.Event) -> bool:
    """Sleep until ``deadline`` (monotonic seconds) or until cancel fires.

    Returns ``True`` if the sleep was interrupted by cancel, ``False``
    if the deadline elapsed normally. The two-arm select mirrors the
    Rust ``tokio::select! { cancel.cancelled(); sleep(wait); }``
    pattern.
    """
    now = time.monotonic()
    wait = max(0.0, deadline - now)
    if wait <= 0:
        return cancel.is_set()
    cancel_task = asyncio.create_task(cancel.wait(), name="scheduler-cancel-wait")
    sleep_task = asyncio.create_task(asyncio.sleep(wait), name="scheduler-sleep")
    try:
        done, pending = await asyncio.wait(
            {cancel_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in (cancel_task, sleep_task):
            if not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
    return cancel_task in done


async def _run_job_loop(spec: JobSpec, bus: HookBus, cancel: asyncio.Event) -> None:
    """Per-job tick loop. Mirrors Rust ``runtime::run_job_loop``.

    Responsibilities:

    * compute the next firing relative to *wall clock* ``utcnow``;
    * sleep until then (or until cancel fires);
    * dispatch on the action;
    * loop.

    A schedule that never has another firing (cron expression valid
    but astronomically impossible, e.g. Feb 30) breaks the loop with
    a ``warning`` log — we don't want to busy-spin asking for
    :func:`next_after`.
    """
    from datetime import datetime, timezone as _tz

    _logger.info("scheduler: job loop started", extra={"job": spec.name})
    while True:
        if cancel.is_set():
            _logger.info("scheduler: cancelled; exiting", extra={"job": spec.name})
            return
        now_wall = datetime.now(tz=_tz.utc)
        nxt = next_after(spec.cron, now_wall)
        if nxt is None:
            _logger.warning(
                "scheduler: cron has no upcoming firing; exiting job loop",
                extra={"job": spec.name},
            )
            return
        wait_secs = max(0.0, (nxt - now_wall).total_seconds())
        deadline_mono = time.monotonic() + wait_secs
        _logger.debug(
            "scheduler: next firing computed",
            extra={
                "job": spec.name,
                "next_fire_at": nxt.isoformat(),
                "wait_secs": int(wait_secs),
            },
        )
        cancelled = await _sleep_until(deadline_mono, cancel)
        if cancelled:
            _logger.info(
                "scheduler: cancelled while sleeping; exiting", extra={"job": spec.name}
            )
            return
        # Re-check the cancel flag before firing — the sleep could
        # have completed in the same tick as a cancel signal. Mirrors
        # the Rust ``if cancel.is_cancelled() { return; }`` guard.
        if cancel.is_set():
            _logger.info(
                "scheduler: cancelled before fire; exiting", extra={"job": spec.name}
            )
            return
        await dispatch(spec, bus)


def spawn(
    cfg: SchedulerConfig,
    bus: HookBus,
    cancel: asyncio.Event | None = None,
) -> SchedulerHandle:
    """Spawn one tick task per ``cfg.jobs`` entry.

    Returns a :class:`SchedulerHandle` aggregating the per-job tasks.
    Jobs whose cron fails to parse are dropped with a warning; the
    rest of the scheduler continues. A config with zero parseable
    jobs returns a handle with an empty task list (no-op scheduler).

    Mirrors the Rust :func:`spawn` 1:1 except for one Python-flavour
    convenience: ``cancel`` is optional — when omitted we make a
    fresh :class:`asyncio.Event` so unit tests can spawn a scheduler
    without threading a cancel event through. The gateway shutdown
    path always passes its own.
    """
    if cancel is None:
        cancel = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    for job in cfg.jobs:
        spec = JobSpec.from_config(job)
        if spec is None:
            continue
        tasks.append(
            asyncio.create_task(
                _run_job_loop(spec, bus, cancel), name=f"scheduler-{job.name}"
            )
        )
    return SchedulerHandle(tasks=tasks, cancel=cancel)


__all__ = [
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
]
