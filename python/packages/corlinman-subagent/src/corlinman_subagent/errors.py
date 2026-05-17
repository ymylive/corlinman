"""Error types for the subagent supervisor.

Mirrors the Rust ``AcquireReject`` enum + ``BridgeError`` enum from
``rust/crates/corlinman-subagent``. On the Python plane we don't have a
PyO3 FFI seam to fold errors across, so the surface collapses to:

- :class:`AcquireReject` — enum of cap-rejection reasons returned by
  :meth:`Supervisor.try_acquire` (mirrors the Rust enum exactly).
- :class:`SubagentError` — umbrella exception base, kept so callers can
  ``except SubagentError`` regardless of which sub-failure happened.
- :class:`AcquireRejectError` — exception flavour of an
  ``AcquireReject``. Raised by :meth:`Supervisor.spawn_child` so the
  async wrapper can unwind out of a rejected spawn the same way an
  agent exception would; the convenience wrapper
  :meth:`Supervisor.spawn_child_to_result` catches it and folds into a
  rejected :class:`~corlinman_subagent.types.TaskResult`.
- :class:`SubagentTimeoutError` — internal sentinel that
  :func:`asyncio.wait_for` raised; the supervisor catches it and folds
  into a :class:`~corlinman_subagent.types.TaskResult` with
  ``finish_reason=TIMEOUT``.
- :class:`BridgeError` — name retained for parity with the Rust crate's
  public surface; aliased to :class:`SubagentError` so the test suite
  / docs match the Rust import names.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AcquireReject",
    "AcquireRejectError",
    "BridgeError",
    "SubagentError",
    "SubagentTimeoutError",
]


class AcquireReject(str, Enum):
    """Mirror of the Rust ``AcquireReject`` enum.

    Reason :meth:`Supervisor.try_acquire` refused. Mapped to
    :class:`~corlinman_subagent.types.FinishReason` at the call site so
    we don't import this enum into the wire types module.
    """

    #: ``parent_ctx.depth >= policy.max_depth``.
    DEPTH_CAPPED = "depth_capped"
    #: Per-parent counter at or above ``max_concurrent_per_parent``.
    PARENT_CONCURRENCY_EXCEEDED = "parent_concurrency_exceeded"
    #: Per-tenant counter at or above ``max_concurrent_per_tenant``.
    TENANT_QUOTA_EXCEEDED = "tenant_quota_exceeded"


class SubagentError(Exception):
    """Base failure raised from the supervisor / agent bridge."""


class AcquireRejectError(SubagentError):
    """A cap rejected the spawn before the agent callable was invoked.

    The :attr:`reason` mirrors :class:`AcquireReject`; the supervisor's
    convenience wrapper unwraps this into a rejected
    :class:`~corlinman_subagent.types.TaskResult` so callers never have
    to think about which kind of failure happened.
    """

    def __init__(self, reason: AcquireReject) -> None:
        super().__init__(f"supervisor rejected acquire: {reason.value}")
        self.reason = reason


class SubagentTimeoutError(SubagentError):
    """The agent callable exceeded its ``max_wall_seconds`` budget.

    Internal sentinel — the supervisor folds this into a
    :class:`~corlinman_subagent.types.TaskResult` with
    ``finish_reason=TIMEOUT`` before it crosses back to the parent
    loop. Kept as a public symbol so test code can assert on it when
    the inner agent does not catch :class:`asyncio.CancelledError`
    cleanly.
    """

    def __init__(self, elapsed_ms: int, budget_seconds: int) -> None:
        super().__init__(
            f"subagent exceeded wall-clock budget "
            f"({elapsed_ms}ms > {budget_seconds * 1000}ms)"
        )
        self.elapsed_ms = elapsed_ms
        self.budget_seconds = budget_seconds


# Parity alias with the Rust crate's ``BridgeError`` umbrella. On the
# Python plane there is no PyO3 bridge so the distinction collapses;
# callers that wrote ``except BridgeError`` against the Rust-shaped API
# get the same behaviour from the umbrella :class:`SubagentError`.
BridgeError = SubagentError
