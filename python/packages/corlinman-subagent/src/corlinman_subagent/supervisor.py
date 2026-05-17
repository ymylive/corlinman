"""Concurrency / depth / timeout caps for ``subagent.spawn``.

Python port of ``rust/crates/corlinman-subagent/src/supervisor.rs``. The
Rust crate split the supervisor (caps + lifecycle) from the Python
``run_child`` runner and bridged the two via PyO3. On the Python plane
both halves live in-process, so this module fuses the cap accountant
*and* the (formerly PyO3-bridged) spawn entry point into a single
``Supervisor`` class.

Three caps, all enforced at :meth:`Supervisor.try_acquire` time:

* **Per-parent concurrency** (default 3) — keyed by
  ``parent_session_key``. One operator session can fan out at most N
  children at any instant; siblings must finish before the (N+1)th.
* **Per-tenant quota** (default 15) — keyed by ``tenant_id``. Stops one
  noisy tenant from starving siblings under shared deployment.
* **Depth cap** (default 2) — ``parent_ctx.depth >= max_depth`` refuses
  the spawn outright. Prevents fork-bomb chains.

A fourth cap, **wall-clock timeout** (``task.max_wall_seconds`` capped
by ``policy.max_wall_seconds_ceiling``), is enforced inside
:meth:`Supervisor.spawn_child` via :func:`asyncio.wait_for`. Timeouts
fold into a :class:`~corlinman_subagent.types.TaskResult` with
``finish_reason=TIMEOUT``; the parent loop never sees an exception.

The slot returned on success is a context-manager drop-guard: entering
the ``with`` block holds the cap counts, exiting (success, error, or
exception) decrements both per-parent and per-tenant counters. Callers
that don't use the ``with`` form get the same behaviour from
``Slot.release()`` (idempotent) and from ``__del__`` as a last-resort
finaliser (mirrors the Rust ``Drop`` guarantee — neither path
double-decrements).

The supervisor uses :class:`asyncio.Lock` around the counter
read-modify-write so two concurrent ``try_acquire`` calls on the same
parent see consistent counts. This is the Python analogue of the Rust
``DashMap`` per-key entry guard; on a single-threaded asyncio runtime
the lock is only contended across ``await`` boundaries.

Hook bus integration mirrors the Rust crate's iter-9 wiring: an
optional :class:`~corlinman_hooks.HookBus` lets the supervisor emit
``SubagentSpawned`` / ``SubagentCompleted`` / ``SubagentTimedOut`` /
``SubagentDepthCapped`` lifecycle events. The bus is optional so unit
tests don't need to stand up a bus, and emits use the fire-and-forget
:meth:`HookBus.emit_nonblocking` path so a slow listener can never
back-pressure the supervisor.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from corlinman_subagent.errors import (
    AcquireReject,
    AcquireRejectError,
    SubagentError,
)
from corlinman_subagent.types import (
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
)

__all__ = [
    "AgentCallable",
    "Slot",
    "Supervisor",
    "SupervisorPolicy",
]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SupervisorPolicy:
    """Policy knobs for the cap accountant.

    Mirrors the Rust ``SupervisorPolicy``. Defaults match the design's
    ``[subagent]`` block:

    - ``max_concurrent_per_parent=3``
    - ``max_concurrent_per_tenant=15``
    - ``max_depth=2``
    - ``max_wall_seconds_ceiling=60`` (used by
      :meth:`Supervisor.spawn_child` to clamp ``task.max_wall_seconds``;
      mirrors ``types::defaults::DEFAULT_MAX_WALL_SECONDS``)
    """

    max_concurrent_per_parent: int = 3
    max_concurrent_per_tenant: int = 15
    max_depth: int = 2
    #: Hard ceiling on a child's wall-clock budget. ``TaskSpec.max_wall_seconds``
    #: may *lower* this but never raise it. Not in the Rust ``SupervisorPolicy``
    #: struct (Rust enforced ceiling externally); we keep it here so the
    #: timeout layer has one struct to consult.
    max_wall_seconds_ceiling: int = DEFAULT_MAX_WALL_SECONDS


# ---------------------------------------------------------------------------
# AgentCallable protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentCallable(Protocol):
    """Async callable shape the supervisor invokes after the cap check.

    Replaces the Rust PyO3 bridge's ``runner_callable``: any async
    function matching ``(spec, child_ctx) -> Awaitable[TaskResult]``
    satisfies the protocol. The supervisor passes the *child's*
    :class:`ParentContext` (already incremented via
    :meth:`ParentContext.child_context`) so the callable doesn't have
    to re-derive ids.
    """

    def __call__(
        self,
        spec: TaskSpec,
        child_ctx: ParentContext,
    ) -> Awaitable[TaskResult]: ...


# ---------------------------------------------------------------------------
# Slot — drop-guard for an acquired concurrency reservation.
# ---------------------------------------------------------------------------


class Slot:
    """Drop-guard for an acquired :class:`Supervisor` slot.

    Construct via :meth:`Supervisor.try_acquire`. Release on context-
    manager exit, an explicit :meth:`release`, or — last resort —
    ``__del__``. Releasing decrements both per-parent and per-tenant
    counters atomically.

    ``release()`` is idempotent. Mirrors the Rust ``Slot`` drop-guard.
    """

    __slots__ = ("_parent_key", "_released", "_supervisor", "_tenant_key")

    def __init__(
        self,
        supervisor: Supervisor,
        parent_key: str,
        tenant_key: str,
    ) -> None:
        self._supervisor = supervisor
        self._parent_key = parent_key
        self._tenant_key = tenant_key
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Explicit release. Idempotent — safe to call multiple times."""
        if self._released:
            return
        self._released = True
        self._supervisor._dec_parent(self._parent_key)
        self._supervisor._dec_tenant(self._tenant_key)

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover — finaliser belt-and-braces
        # Last-resort release if a caller drops the slot without using
        # the ``with`` form. Mirrors the Rust ``Drop`` guarantee. Wrap
        # in try/except so a torn-down event loop / supervisor doesn't
        # surface a finaliser exception during interpreter shutdown.
        try:
            self.release()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Cap accountant + spawn entry point.

    Construct once at gateway boot, share by reference. Cloning is not
    needed (the per-key counter dicts are owned by the instance).

    The optional ``hook_bus`` lets the supervisor emit lifecycle events
    on each spawn / completion / rejection. Emits are best-effort:
    failures are swallowed so a slow listener can never break a spawn.
    """

    __slots__ = (
        "_hook_bus",
        "_lock",
        "_per_parent",
        "_per_tenant",
        "_policy",
    )

    def __init__(
        self,
        policy: SupervisorPolicy | None = None,
        *,
        hook_bus: Any | None = None,
    ) -> None:
        """Initialise with ``policy`` (defaults applied if omitted).

        ``hook_bus`` is typed as :class:`typing.Any` so the import
        stays lazy — the supervisor only needs the duck-typed
        ``emit_nonblocking`` method on the bus. This matches the Rust
        crate's ``Option<Arc<HookBus>>`` field which is ``None`` by
        default; tests that don't care about hooks pass nothing.
        """
        self._policy = policy if policy is not None else SupervisorPolicy()
        self._per_parent: dict[str, int] = {}
        self._per_tenant: dict[str, int] = {}
        self._hook_bus = hook_bus
        # Single asyncio lock guards both counter dicts. The Rust crate
        # uses per-key DashMap entries; on a single-threaded asyncio
        # event loop one global lock is simpler and just as correct
        # (the read-modify-write window doesn't span any ``await``).
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> SupervisorPolicy:
        return self._policy

    @property
    def hook_bus(self) -> Any | None:
        """The installed hook bus, or ``None`` if not wired."""
        return self._hook_bus

    # -- inspection helpers (mirror the Rust ``parent_count`` / ``tenant_count``)

    def parent_count(self, parent_session_key: str) -> int:
        """Current in-flight count for ``parent_session_key``."""
        return self._per_parent.get(parent_session_key, 0)

    def tenant_count(self, tenant_id: str) -> int:
        """Current in-flight count for ``tenant_id``."""
        return self._per_tenant.get(tenant_id, 0)

    # ----------------------------------------------------------------- internal

    def _dec_parent(self, parent_session_key: str) -> None:
        cur = self._per_parent.get(parent_session_key, 0)
        if cur <= 1:
            # Drop the key when the counter reaches zero so the dict
            # doesn't grow unboundedly across long-lived sessions.
            self._per_parent.pop(parent_session_key, None)
        else:
            self._per_parent[parent_session_key] = cur - 1

    def _dec_tenant(self, tenant_id: str) -> None:
        cur = self._per_tenant.get(tenant_id, 0)
        if cur <= 1:
            self._per_tenant.pop(tenant_id, None)
        else:
            self._per_tenant[tenant_id] = cur - 1

    # ----------------------------------------------------------------- acquire

    def try_acquire(self, parent_ctx: ParentContext) -> Slot:
        """Try to reserve a child slot for ``parent_ctx``.

        Returns the acquired :class:`Slot` on success. Raises
        :class:`AcquireRejectError` if any cap is hit. The check order
        mirrors Rust: depth → per-parent → per-tenant. Order matters
        because depth is the cheapest check and tenant-quota telemetry
        belongs closer to the bottom of the funnel.

        Synchronous (no ``await``) because on a single-threaded
        asyncio loop the read-modify-write window does not need to
        yield. The Rust crate is also sync at the cap accountant layer
        (the DashMap entry guard is non-blocking).
        """
        # Depth gate — cheapest and purely on the snapshot.
        if parent_ctx.depth >= self._policy.max_depth:
            self._emit_reject(parent_ctx, AcquireReject.DEPTH_CAPPED)
            raise AcquireRejectError(AcquireReject.DEPTH_CAPPED)

        parent_key = parent_ctx.parent_session_key
        tenant_key = parent_ctx.tenant_id

        # Per-parent admit-or-reject.
        cur_parent = self._per_parent.get(parent_key, 0)
        if cur_parent >= self._policy.max_concurrent_per_parent:
            self._emit_reject(parent_ctx, AcquireReject.PARENT_CONCURRENCY_EXCEEDED)
            raise AcquireRejectError(AcquireReject.PARENT_CONCURRENCY_EXCEEDED)
        self._per_parent[parent_key] = cur_parent + 1

        # Per-tenant. If this fails, roll the per-parent increment back.
        cur_tenant = self._per_tenant.get(tenant_key, 0)
        if cur_tenant >= self._policy.max_concurrent_per_tenant:
            self._dec_parent(parent_key)
            self._emit_reject(parent_ctx, AcquireReject.TENANT_QUOTA_EXCEEDED)
            raise AcquireRejectError(AcquireReject.TENANT_QUOTA_EXCEEDED)
        self._per_tenant[tenant_key] = cur_tenant + 1

        return Slot(self, parent_key, tenant_key)

    # ----------------------------------------------------------------- hooks

    def _safe_emit(self, event: Any) -> None:
        """Best-effort emit via ``hook_bus.emit_nonblocking``.

        Any exception is swallowed (logged-equivalent of the Rust
        ``warn!``). The supervisor must never crash because a listener
        is misbehaving.
        """
        bus = self._hook_bus
        if bus is None:
            return
        try:
            bus.emit_nonblocking(event)
        except Exception:
            # Mirror the Rust crate's "hooks never crash the caller"
            # stance. We deliberately swallow — there is no logger
            # plumbed through this layer.
            pass

    def _emit_reject(
        self,
        parent_ctx: ParentContext,
        reject: AcquireReject,
    ) -> None:
        """Iter-9 emit helper: best-effort ``SubagentDepthCapped`` event
        for every cap-rejected spawn. The variant carries a ``reason``
        field discriminating depth-cap from the concurrency caps so
        dashboards can split the funnel.
        """
        if self._hook_bus is None:
            return
        # Import lazily so this package doesn't pay the corlinman_hooks
        # import cost at module-load time. The hook bus is itself
        # already imported by whatever wired it in, so the symbol is
        # already in sys.modules by the time we reach here in practice.
        from corlinman_hooks import HookEvent  # noqa: PLC0415

        event = HookEvent.SubagentDepthCapped(
            parent_session_key=parent_ctx.parent_session_key,
            attempted_depth=parent_ctx.depth,
            reason=reject.value,
            parent_trace_id=parent_ctx.trace_id,
            tenant_id=parent_ctx.tenant_id,
        )
        self._safe_emit(event)

    def emit_spawned(
        self,
        parent_ctx: ParentContext,
        child_ctx: ParentContext,
        agent_card: str,
    ) -> None:
        """Iter-9 emit helper: ``SubagentSpawned`` once the slot is
        acquired and the child's runtime context is known.
        """
        if self._hook_bus is None:
            return
        from corlinman_hooks import HookEvent  # noqa: PLC0415

        event = HookEvent.SubagentSpawned(
            parent_session_key=parent_ctx.parent_session_key,
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            agent_card=agent_card,
            depth=child_ctx.depth,
            parent_trace_id=parent_ctx.trace_id,
            tenant_id=parent_ctx.tenant_id,
        )
        self._safe_emit(event)

    def emit_finished(
        self,
        parent_ctx: ParentContext,
        result: TaskResult,
    ) -> None:
        """Iter-9 emit helper: ``SubagentCompleted`` / ``SubagentTimedOut``
        based on the child's terminal :class:`TaskResult`.

        Splits ``TIMEOUT`` into its own variant so dashboards can
        red-flag timeouts without parsing the inner ``finish_reason``.
        Pre-spawn rejections (``DEPTH_CAPPED`` / ``REJECTED``) are
        owned by :meth:`_emit_reject` and short-circuited here to
        avoid double-emits.
        """
        if self._hook_bus is None:
            return
        from corlinman_hooks import HookEvent  # noqa: PLC0415

        if result.finish_reason is FinishReason.TIMEOUT:
            event = HookEvent.SubagentTimedOut(
                parent_session_key=parent_ctx.parent_session_key,
                child_session_key=result.child_session_key,
                child_agent_id=result.child_agent_id,
                elapsed_ms=result.elapsed_ms,
                parent_trace_id=parent_ctx.trace_id,
                tenant_id=parent_ctx.tenant_id,
            )
        elif result.finish_reason.is_pre_spawn_rejection():
            # Pre-spawn rejections are owned by emit_reject — calling
            # emit_finished on one of these would double-emit. Drop
            # silently.
            return
        else:
            event = HookEvent.SubagentCompleted(
                parent_session_key=parent_ctx.parent_session_key,
                child_session_key=result.child_session_key,
                child_agent_id=result.child_agent_id,
                finish_reason=result.finish_reason.as_str(),
                elapsed_ms=result.elapsed_ms,
                tool_calls_made=len(result.tool_calls_made),
                parent_trace_id=parent_ctx.trace_id,
                tenant_id=parent_ctx.tenant_id,
            )
        self._safe_emit(event)

    # ----------------------------------------------------------------- spawn

    def _resolve_budget_seconds(self, task: TaskSpec) -> int:
        """Apply the wall-clock ceiling to ``task.max_wall_seconds``.

        Mirrors the design's "``task.max_wall_seconds`` may *lower* the
        ceiling but never raise it" contract. The supervisor enforces
        the upper bound; the spec's value is the caller's request.
        """
        ceiling = self._policy.max_wall_seconds_ceiling
        return min(task.max_wall_seconds, ceiling)

    async def spawn_child(
        self,
        agent: AgentCallable,
        parent_ctx: ParentContext,
        task: TaskSpec,
        *,
        agent_card: str = "<spawned>",
        child_seq: int = 0,
    ) -> TaskResult:
        """Run a child subagent end-to-end.

        1. Acquires a :class:`Slot` (raises :class:`AcquireRejectError`
           on cap rejection — depth / per-parent / per-tenant).
        2. Derives the child :class:`ParentContext` and emits
           ``SubagentSpawned`` on the optional hook bus.
        3. Awaits ``agent(spec, child_ctx)`` under
           :func:`asyncio.wait_for` with the effective wall-clock
           budget. A timeout folds into a
           :class:`TaskResult` with ``finish_reason=TIMEOUT`` rather
           than raising — the parent loop never sees the exception.
        4. Emits ``SubagentCompleted`` / ``SubagentTimedOut`` based on
           the terminal result.
        5. Releases the slot in *every* exit path (success, error,
           timeout, cancellation) via the :class:`Slot` context manager.

        Raises:
            AcquireRejectError: cap rejected the spawn before the
                agent callable was invoked. Wrap with
                :meth:`spawn_child_to_result` to get a rejected
                :class:`TaskResult` envelope instead.
            SubagentError: agent callable raised.
        """
        # Cap check first — cheap, prompt-injection-safe.
        slot = self.try_acquire(parent_ctx)

        # Derive the child's runtime context for the spawn event and
        # for the agent callable. Mirrors the Rust bridge's
        # ``parent_ctx.child_context(&child_card, 0)`` call.
        child_ctx = parent_ctx.child_context(agent_card, child_seq)
        self.emit_spawned(parent_ctx, child_ctx, agent_card)

        budget_s = self._resolve_budget_seconds(task)
        start_ns = time.monotonic_ns()

        try:
            with slot:
                coro = agent(spec=task, child_ctx=child_ctx) if _is_kw_callable(agent) else agent(task, child_ctx)
                try:
                    result = await asyncio.wait_for(coro, timeout=budget_s)
                except asyncio.TimeoutError:
                    elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                    result = TaskResult(
                        output_text="",
                        tool_calls_made=[],
                        # Same convention as the rejected envelope: the
                        # child *did* start (we entered the agent
                        # callable) so we stamp the would-be child ids
                        # rather than the ``::child::-`` placeholder.
                        child_session_key=child_ctx.parent_session_key,
                        child_agent_id=child_ctx.parent_agent_id,
                        elapsed_ms=int(elapsed_ms),
                        finish_reason=FinishReason.TIMEOUT,
                        error=(
                            f"subagent exceeded wall-clock budget "
                            f"({int(elapsed_ms)}ms > {budget_s * 1000}ms)"
                        ),
                    )
        except AcquireRejectError:
            # Should not happen — try_acquire above already returned a
            # Slot. Re-raise so the test suite catches the contract
            # break.
            raise
        except SubagentError:
            # Pass through subagent-specific errors so callers can
            # distinguish them.
            raise
        except Exception as exc:
            # Any other exception inside the agent callable folds into
            # a SubagentError so the caller never has to think about
            # what kind of failure happened. The slot has already
            # released via the ``with slot:`` exit path.
            raise SubagentError(f"agent callable raised: {exc!r}") from exc

        # Emit lifecycle hook based on the terminal result.
        self.emit_finished(parent_ctx, result)
        return result

    async def spawn_child_to_result(
        self,
        agent: AgentCallable,
        parent_ctx: ParentContext,
        task: TaskSpec,
        *,
        agent_card: str = "<spawned>",
        child_seq: int = 0,
    ) -> TaskResult:
        """Convenience wrapper around :meth:`spawn_child`.

        Folds every failure mode into a :class:`TaskResult` envelope so
        the gateway dispatcher never has to branch on exception type.

        Mapping:

        * :class:`AcquireRejectError` (``DEPTH_CAPPED``) →
          ``TaskResult.rejected(DEPTH_CAPPED, ...)``
        * :class:`AcquireRejectError`
          (``PARENT_CONCURRENCY_EXCEEDED`` / ``TENANT_QUOTA_EXCEEDED``)
          → ``TaskResult.rejected(REJECTED, ...)``
        * :class:`SubagentError` / any other exception →
          ``TaskResult(finish_reason=ERROR, error=msg)``

        Mirrors the Rust ``spawn_child_to_result`` helper.
        """
        try:
            return await self.spawn_child(
                agent,
                parent_ctx,
                task,
                agent_card=agent_card,
                child_seq=child_seq,
            )
        except AcquireRejectError as e:
            if e.reason is AcquireReject.DEPTH_CAPPED:
                return TaskResult.rejected(
                    FinishReason.DEPTH_CAPPED,
                    parent_ctx.parent_session_key,
                    "subagent depth cap reached",
                )
            return TaskResult.rejected(
                FinishReason.REJECTED,
                parent_ctx.parent_session_key,
                f"supervisor rejected: {e.reason.value}",
            )
        except Exception as exc:
            # Folded error envelope — mirrors the Rust
            # ``BridgeError::PythonError`` arm.
            return TaskResult(
                output_text="",
                tool_calls_made=[],
                child_session_key=f"{parent_ctx.parent_session_key}::child::-",
                child_agent_id="",
                elapsed_ms=0,
                finish_reason=FinishReason.ERROR,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_kw_callable(agent: Callable[..., Any]) -> bool:
    """Return ``True`` if ``agent`` accepts ``spec`` / ``child_ctx`` by
    keyword.

    The :class:`AgentCallable` Protocol uses keyword names ``spec`` and
    ``child_ctx`` for readability; production agent runners may also
    accept positional args (``lambda spec, ctx: ...``). We try to
    detect the keyword-args case so both styles work without forcing
    one in the public surface.

    Falls back to positional call if introspection fails (e.g. C-
    implemented callables). This is a best-effort optimisation — the
    AgentCallable Protocol explicitly documents keyword names so
    production callers should always satisfy the keyword path.
    """
    try:
        sig = inspect.signature(agent)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    return "spec" in params and "child_ctx" in params
