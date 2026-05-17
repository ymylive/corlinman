"""Port of ``corlinman-scheduler::jobs`` unit tests to pytest.

Mirrors the Rust ``mod tests`` in ``src/jobs.rs``:

* ``drops_invalid_cron``
* ``maps_subprocess_fields``
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.scheduler import (
    ActionSpec,
    JobAction,
    JobSpec,
    SchedulerJob,
)


def _job(cron: str, action: JobAction) -> SchedulerJob:
    """Test helper — build a :class:`SchedulerJob` with a fixed name.

    Mirrors the Rust ``cfg`` helper at the top of ``jobs.rs``'s test
    module. ``timezone`` stays unset (the Python port treats it as
    advisory; the runtime evaluates everything in UTC)."""
    return SchedulerJob(name="t", cron=cron, action=action)


def test_drops_invalid_cron() -> None:
    """:meth:`JobSpec.from_config` returns ``None`` (and logs a warning;
    we don't assert the log here) when the cron expression won't parse.
    Caller drops the job — port of the Rust behaviour where one bad
    job can't take the whole scheduler down."""
    j = _job(
        "not a cron",
        JobAction.subprocess(command="true", args=(), timeout_secs=60),
    )
    assert JobSpec.from_config(j) is None


def test_maps_subprocess_fields() -> None:
    """Every field on the Subprocess action makes it through the
    config → JobSpec conversion intact: command, args, timeout,
    working_dir, env (one entry pinned for an exact assertion)."""
    j = _job(
        "0 0 3 * * * *",
        JobAction.subprocess(
            command="echo",
            args=("hi",),
            timeout_secs=5,
            working_dir=Path("/tmp"),
            env={"FOO": "bar"},
        ),
    )
    spec = JobSpec.from_config(j)
    assert spec is not None
    assert spec.name == "t"
    assert isinstance(spec.action, ActionSpec)
    assert spec.action.kind == "subprocess"
    assert spec.action.command == "echo"
    assert spec.action.args == ("hi",)
    assert spec.action.timeout_secs == 5
    assert spec.action.working_dir == Path("/tmp")
    assert spec.action.env.get("FOO") == "bar"


def test_run_agent_action_round_trip() -> None:
    """``RunAgent`` carries its prompt through unchanged. The runtime
    will reject the action at dispatch time (unsupported_action), but
    the config conversion must still produce a spec so the rejection
    happens on the bus, not silently."""
    j = _job("0 0 3 * * * *", JobAction.run_agent(prompt="hello"))
    spec = JobSpec.from_config(j)
    assert spec is not None
    assert spec.action.kind == "run_agent"
    assert spec.action.prompt == "hello"


def test_run_tool_action_round_trip() -> None:
    """``RunTool`` carries plugin/tool/args through unchanged. Same
    rationale as the RunAgent test above."""
    j = _job(
        "0 0 3 * * * *", JobAction.run_tool(plugin="p", tool="t", args={"x": 1})
    )
    spec = JobSpec.from_config(j)
    assert spec is not None
    assert spec.action.kind == "run_tool"
    assert spec.action.plugin == "p"
    assert spec.action.tool == "t"
    assert spec.action.tool_args == {"x": 1}


def test_subprocess_action_defaults_match_rust() -> None:
    """``timeout_secs`` defaults to 600 (the Rust serde default of
    ``default_subprocess_timeout_secs``). ``env`` defaults to empty,
    ``working_dir`` to ``None``, ``args`` to empty tuple."""
    a = JobAction.subprocess(command="true")
    assert a.timeout_secs == 600
    assert a.working_dir is None
    assert a.args == ()
    assert dict(a.env) == {}
