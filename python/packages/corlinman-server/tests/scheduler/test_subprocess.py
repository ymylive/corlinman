"""Port of ``corlinman-scheduler::subprocess`` unit tests to pytest.

Mirrors the Rust ``mod tests`` in ``src/subprocess.rs``:

* ``success_on_zero_exit``
* ``non_zero_on_false``
* ``timeout_kills_long_runner``
* ``spawn_failed_for_missing_binary``
* ``env_is_passed_to_child``

The tests use POSIX ``true`` / ``false`` / ``sleep`` / ``sh`` binaries.
These ship with every Linux/macOS CI image the gateway runs on; if
the test suite ever expands to Windows runners we'll need a different
fixture (``cmd /c exit 0`` etc.) — until then the parity with the
Rust suite is exact.
"""

from __future__ import annotations

from corlinman_server.scheduler import (
    SubprocessOutcome,
    SubprocessOutcomeKind,
    run_subprocess,
)


async def test_success_on_zero_exit() -> None:
    """``true`` exits 0 → :attr:`SubprocessOutcomeKind.SUCCESS`."""
    out = await run_subprocess("test", "run-1", "true", (), 5, None, {})
    assert isinstance(out, SubprocessOutcome)
    assert out.kind is SubprocessOutcomeKind.SUCCESS
    assert out.duration_secs >= 0


async def test_non_zero_on_false() -> None:
    """``false`` exits 1 → :attr:`SubprocessOutcomeKind.NON_ZERO_EXIT`
    with ``exit_code == 1``. POSIX guarantees the exit code on every
    Linux/macOS userland the suite runs against."""
    out = await run_subprocess("test", "run-2", "false", (), 5, None, {})
    assert out.kind is SubprocessOutcomeKind.NON_ZERO_EXIT
    assert out.exit_code == 1


async def test_timeout_kills_long_runner() -> None:
    """``sleep 30`` with a 1s timeout → :attr:`SubprocessOutcomeKind.TIMEOUT`.
    The runner SIGKILLs the child so the test process doesn't hang for
    30 seconds waiting on the child to finish."""
    out = await run_subprocess("test", "run-3", "sleep", ("30",), 1, None, {})
    assert out.kind is SubprocessOutcomeKind.TIMEOUT


async def test_spawn_failed_for_missing_binary() -> None:
    """A binary that doesn't exist surfaces as
    :attr:`SubprocessOutcomeKind.SPAWN_FAILED` with the OS error
    captured in ``error`` — caller emits ``EngineRunFailed`` with
    ``error_kind = "spawn_failed"``."""
    out = await run_subprocess(
        "test", "run-4", "/nonexistent/__corlinman_test__", (), 5, None, {}
    )
    assert out.kind is SubprocessOutcomeKind.SPAWN_FAILED
    assert out.error is not None


async def test_env_is_passed_to_child() -> None:
    """``sh -c 'test "$FOO" = bar'`` exits 0 iff ``FOO`` is ``bar``.
    Proves the ``env`` map is merged over the inherited environment
    on the way to the child — matches the Rust ``Command::env``
    semantics where the explicit entries override but PATH/etc.
    inherit."""
    out = await run_subprocess(
        "test",
        "run-5",
        "sh",
        ("-c", 'test "$FOO" = bar'),
        5,
        None,
        {"FOO": "bar"},
    )
    assert out.kind is SubprocessOutcomeKind.SUCCESS, (
        f"child should see FOO=bar; got {out!r}"
    )
