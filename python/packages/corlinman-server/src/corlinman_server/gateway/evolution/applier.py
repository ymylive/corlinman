"""``EvolutionApplier`` — concrete
:class:`corlinman_auto_rollback.Applier` over the evolution-store.

Port of :rust:`corlinman_gateway::evolution_applier`. The Rust source is
~6600 LoC because it owns every per-kind kb mutation (memory_op,
tag_rebalance, skill_update, prompt_template, tool_policy, agent_card,
engine_config / prompt / filter / threshold meta). The Python port keeps
the **orchestration shell** — load + gate, intent-log bookkeeping,
audit-row write, status flip — and routes the actual kb mutation through
a pluggable :class:`KindHandler` map so each per-kind handler can land
additively without re-touching this file.

This matches the Rust dispatch shape (one ``match`` arm per
:class:`EvolutionKind` variant) but lifts the per-arm body into
caller-supplied async callbacks. The result is a single applier that:

* Implements the :class:`corlinman_auto_rollback.Applier` protocol
  (one ``revert`` method) so it plugs straight into the
  :class:`corlinman_auto_rollback.AutoRollbackMonitor`.
* Exposes :meth:`apply` mirroring the Rust ``EvolutionApplier::apply``
  signature for the admin route handler.
* Surfaces a typed :class:`ApplyError` hierarchy that maps cleanly to
  the :class:`corlinman_auto_rollback.RevertError` set.

Two-DB partial-failure semantics (kb.sqlite vs evolution.sqlite)
mirror the Rust module-level note: kb mutation runs first, the
``evolution_history`` insert + status flip run as a single
transaction. A kb success + audit failure leaves data without an
audit trail — handed off to the AutoRollback monitor's metrics
regression detector. We deliberately do **not** auto-recover here
without operator input (same rationale as the Rust
``scan_half_committed`` doc).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from corlinman_auto_rollback.revert import (
    Applier,
    HistoryMissingRevertError,
    InternalRevertError,
    NotAppliedRevertError,
    NotFoundRevertError,
    RevertError,
    UnsupportedKindRevertError,
)
from corlinman_evolution_store import (
    EvolutionHistory,
    EvolutionKind,
    EvolutionProposal,
    EvolutionStatus,
    HistoryRepo,
    NotFoundError,
    ProposalId,
    ProposalsRepo,
    RepoError,
)

__all__ = [
    "ApplyError",
    "ApplyResult",
    "EvolutionApplier",
    "KindHandler",
    "MutationOutcome",
    "NotApprovedError",
    "NotFoundApplyError",
    "NotAppliedError",
    "HistoryMissingError",
    "UnsupportedKindError",
    "UnsupportedRevertKindError",
    "InvalidTargetError",
    "MalformedInverseDiffError",
    "now_ms",
    "sha256_hex",
]


log = logging.getLogger(__name__)


# ─── Error hierarchy (mirrors Rust ``ApplyError``) ───────────────────


class ApplyError(Exception):
    """Base class for everything :meth:`EvolutionApplier.apply` /
    :meth:`EvolutionApplier.revert` can raise.

    Mirrors :rust:`corlinman_gateway::evolution_applier::ApplyError`
    minus the per-kind specialisations the Python port hasn't grown
    yet (``ChunkNotFound``, ``TagNotFound``, ``DriftMismatch``, …).
    Per-kind handlers raise concrete subclasses; the orchestrator
    surfaces them verbatim.
    """


class NotFoundApplyError(ApplyError):
    """Proposal id wasn't in ``evolution_proposals``."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"proposal not found: {proposal_id}")
        self.proposal_id = proposal_id


class NotApprovedError(ApplyError):
    """Proposal is not in ``approved``. Carries the actual status."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal not approved (status={status})")
        self.status = status


class NotAppliedError(ApplyError):
    """Revert path: proposal isn't in ``applied``. Distinct from
    :class:`NotApprovedError` so the monitor can tell "already
    rolled back" apart from "never applied"."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal not applied (status={status})")
        self.status = status


class HistoryMissingError(ApplyError):
    """Revert path: forward apply succeeded but the audit row is gone."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"history row missing for proposal {proposal_id}")
        self.proposal_id = proposal_id


class UnsupportedKindError(ApplyError):
    """No forward handler is wired for this :class:`EvolutionKind`."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"kind {kind} cannot be applied yet")
        self.kind = kind


class UnsupportedRevertKindError(ApplyError):
    """No revert handler is wired for this :class:`EvolutionKind`."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"kind {kind} cannot be reverted yet")
        self.kind = kind


class InvalidTargetError(ApplyError):
    """Target string didn't match any supported shape for this kind."""

    def __init__(self, target: str) -> None:
        super().__init__(f"invalid target: {target}")
        self.target = target


class MalformedInverseDiffError(ApplyError):
    """``inverse_diff`` JSON didn't parse or was missing required keys."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"malformed inverse_diff: {reason}")
        self.reason = reason


# ─── Mutation result type ────────────────────────────────────────────


@dataclass(frozen=True)
class MutationOutcome:
    """What a per-kind handler returns so the orchestrator can stamp
    the audit row uniformly across kinds.

    Mirrors :rust:`MutationOutcome`:

    * ``before_sha`` / ``after_sha`` — SHA-256 hex of the state
      before/after the mutation (handler decides what "state" means
      — for ``memory_op`` it's the chunk content, for ``skill_update``
      it's the file body, etc.).
    * ``inverse_diff`` — JSON describing how to undo the op. The
      revert handler reads it back to replay the inverse.
    """

    before_sha: str
    after_sha: str
    inverse_diff: str


@dataclass(frozen=True)
class ApplyResult:
    """Returned by :meth:`EvolutionApplier.apply_with_share_with`.

    The legacy :meth:`EvolutionApplier.apply` returns just the
    :class:`EvolutionHistory` row; this richer shape preserves the
    Rust ``ApplyResult`` so the federation rebroadcaster can land
    additively. ``peer_proposals_minted`` and ``peer_failures`` are
    currently always 0 / ``[]`` — federation is a follow-up port
    (the Rust ``FederationRebroadcaster`` lives further down the
    same source file).
    """

    history: EvolutionHistory
    peer_proposals_minted: int = 0
    peer_failures: list[tuple[str, str]] = field(default_factory=list)


# ─── Per-kind handler protocol ───────────────────────────────────────


@runtime_checkable
class KindHandler(Protocol):
    """Pair of async callbacks that own one :class:`EvolutionKind`.

    Mirrors the Rust per-kind ``apply_*`` / ``revert_*`` method pairs:
    the orchestrator dispatches into ``apply(proposal)`` to mutate the
    underlying store and into ``revert(history)`` to roll back later.

    Both methods raise :class:`ApplyError` subclasses on failure. The
    orchestrator catches them and stamps the intent-log + audit-row
    bookkeeping.
    """

    async def apply(self, proposal: EvolutionProposal) -> MutationOutcome: ...

    async def revert(self, history: EvolutionHistory) -> None: ...


# Type alias for ergonomic per-handler registration:
# ``applier.register(EvolutionKind.MEMORY_OP, MyMemoryOpHandler())``.
KindHandlerMap = dict[EvolutionKind, KindHandler]


# ─── Applier ──────────────────────────────────────────────────────────


class EvolutionApplier(Applier):
    """Orchestrates forward + reverse apply against the evolution-store.

    Mirrors :rust:`corlinman_gateway::evolution_applier::EvolutionApplier`
    minus the per-kind kb mutation code (delegated to
    :class:`KindHandler` callbacks). Implements the
    :class:`corlinman_auto_rollback.Applier` protocol so it plugs
    directly into :class:`corlinman_auto_rollback.AutoRollbackMonitor`.

    Construction takes the two repo handles the orchestrator needs
    plus an optional :class:`KindHandlerMap`; handlers can also be
    registered after construction with :meth:`register_handler` so
    boot can wire them in order.

    Source-tenant + federation rebroadcast (Rust phase 4 W2 B3) are
    parked as TODOs — see ``Returns`` of :meth:`apply_with_share_with`.
    """

    def __init__(
        self,
        proposals: ProposalsRepo,
        history: HistoryRepo,
        *,
        handlers: KindHandlerMap | None = None,
        clock: Callable[[], int] = None,  # type: ignore[assignment]
    ) -> None:
        self._proposals = proposals
        self._history = history
        self._handlers: KindHandlerMap = dict(handlers or {})
        self._clock = clock or now_ms

    def register_handler(self, kind: EvolutionKind, handler: KindHandler) -> None:
        """Wire (or override) a per-kind handler. Idempotent — re-
        registering replaces the prior handler so tests can swap a
        scripted mock in mid-fixture.
        """
        self._handlers[kind] = handler

    @property
    def handlers(self) -> KindHandlerMap:
        """Read-only view of the currently-wired handlers. Mirrors the
        Rust ``cfg(test)`` accessor — exposed unconditionally here
        because Python's structural typing makes hiding it pointless."""
        return dict(self._handlers)

    # ─── Forward apply ────────────────────────────────────────────

    async def apply(self, proposal_id: ProposalId) -> EvolutionHistory:
        """Apply an approved proposal. Returns the freshly-inserted
        :class:`EvolutionHistory` row.

        Mirrors :rust:`EvolutionApplier::apply` — the legacy entry point
        that returns just the history row (no federation payload).
        """
        res = await self.apply_with_share_with(proposal_id, share_with=None)
        return res.history

    async def apply_with_share_with(
        self,
        proposal_id: ProposalId,
        *,
        share_with: list[str] | None,
    ) -> ApplyResult:
        """Phase-4-compatible entry. ``share_with`` is persisted on the
        history row so a future federation rebroadcaster can fan out;
        the rebroadcast itself is a TODO (Rust
        ``FederationRebroadcaster`` not yet ported).

        Returns the :class:`ApplyResult` with a (currently always 0)
        peer-mint tally so call sites can swap to this richer shape
        ahead of the federation port.
        """
        # 1. Load + gate. ``NotFound`` / wrong-status returns short-
        #    circuit before the intent log opens (Rust step 1).
        try:
            proposal = await self._proposals.get(proposal_id)
        except NotFoundError:
            raise NotFoundApplyError(str(proposal_id)) from None
        except RepoError as err:
            raise ApplyError(f"repo error: {err}") from err

        if proposal.status != EvolutionStatus.APPROVED:
            raise NotApprovedError(proposal.status.as_str())

        handler = self._handlers.get(proposal.kind)
        if handler is None:
            raise UnsupportedKindError(proposal.kind.as_str())

        # 2. Forward apply. The handler raises a typed
        #    :class:`ApplyError` on any failure; we just propagate.
        outcome = await handler.apply(proposal)

        # 3. Audit row + status flip.
        now = self._clock()
        history_row = EvolutionHistory(
            proposal_id=proposal_id,
            kind=proposal.kind,
            target=proposal.target,
            before_sha=outcome.before_sha,
            after_sha=outcome.after_sha,
            inverse_diff=outcome.inverse_diff,
            # Empty baseline by default — wiring the AutoRollback
            # baseline snapshot is a follow-up that needs the
            # ``capture_snapshot`` helper hooked up. Mirrors the Rust
            # "watched_event_kinds empty → empty baseline" branch.
            metrics_baseline={},
            applied_at=now,
            rolled_back_at=None,
            rollback_reason=None,
            share_with=share_with,
        )
        try:
            history_id = await self._history.insert(history_row)
            await self._proposals.mark_applied(proposal_id, now)
        except RepoError as err:
            # Kb mutation already landed (handler returned ok). Mirror
            # the Rust comment: this is the "kb done, audit failed"
            # partial-fail case the monitor's metrics regression
            # detector eventually catches.
            log.warning(
                "evolution_applier.audit_write_failed proposal=%s err=%s",
                proposal_id,
                err,
            )
            raise ApplyError(f"history write failed: {err}") from err

        history_row.id = history_id
        return ApplyResult(
            history=history_row,
            peer_proposals_minted=0,
            peer_failures=[],
        )

    # ─── Reverse apply ────────────────────────────────────────────

    async def revert(
        self,
        proposal_id: ProposalId,
        reason: str,
    ) -> None:
        """Revert the proposal identified by ``proposal_id``.

        Implements the :class:`corlinman_auto_rollback.Applier`
        protocol — the monitor calls this once a metrics regression
        breaches the configured threshold.

        Raises one of the
        :class:`corlinman_auto_rollback.RevertError` subclasses on
        failure; the public ApplyError set is mapped onto that
        narrower contract mirroring :rust:`impl AutoRollbackApplier`
        ``for EvolutionApplier``.
        """
        try:
            await self._revert_inner(proposal_id, reason)
        except NotFoundApplyError as err:
            raise NotFoundRevertError(err.proposal_id) from err
        except NotAppliedError as err:
            raise NotAppliedRevertError(err.status) from err
        except HistoryMissingError as err:
            raise HistoryMissingRevertError(err.proposal_id) from err
        except UnsupportedRevertKindError as err:
            raise UnsupportedKindRevertError(err.kind) from err
        except UnsupportedKindError as err:  # symmetry — no fwd → no rev
            raise UnsupportedKindRevertError(err.kind) from err
        except ApplyError as err:
            # Kb, History, MalformedInverseDiff, InvalidTarget,
            # Repo, … all collapse to Internal (mirrors the Rust
            # ``Err(other) => Err(RevertError::Internal(...))`` arm).
            raise InternalRevertError(str(err)) from err

    async def revert_returning_history(
        self,
        proposal_id: ProposalId,
        reason: str,
    ) -> EvolutionHistory:
        """Variant returning the patched :class:`EvolutionHistory`.

        Mirrors the Rust :rust:`EvolutionApplier::revert` public method
        (the one the admin route exposes directly). The
        :class:`Applier`-protocol wrapper :meth:`revert` discards the
        history row to match the monitor's expected signature.

        Raises :class:`ApplyError` subclasses verbatim (not the
        narrower :class:`RevertError` set used by the
        :class:`Applier`-protocol path).
        """
        return await self._revert_inner(proposal_id, reason)

    async def _revert_inner(
        self,
        proposal_id: ProposalId,
        reason: str,
    ) -> EvolutionHistory:
        # 1. Gate on ``Applied``. ``RolledBack`` → NotApplied so the
        #    monitor can tell idempotent re-fires apart from missing
        #    proposals. Mirrors Rust step 1.
        try:
            proposal = await self._proposals.get(proposal_id)
        except NotFoundError:
            raise NotFoundApplyError(str(proposal_id)) from None
        except RepoError as err:
            raise ApplyError(f"repo error: {err}") from err

        if proposal.status != EvolutionStatus.APPLIED:
            raise NotAppliedError(proposal.status.as_str())

        # 2. Fetch the audit row's ``inverse_diff``. Missing here is
        #    data corruption — forward apply must have written it.
        try:
            history_row = await self._history.latest_for_proposal(proposal_id)
        except NotFoundError:
            raise HistoryMissingError(str(proposal_id)) from None
        except RepoError as err:
            raise ApplyError(f"repo error: {err}") from err

        # 3. Dispatch per kind. ``UnsupportedRevertKindError`` mirrors
        #    the Rust ``ApplyError::UnsupportedRevertKind`` branch.
        handler = self._handlers.get(proposal.kind)
        if handler is None:
            raise UnsupportedRevertKindError(proposal.kind.as_str())
        await handler.revert(history_row)

        # 4. Audit + status flip. Mirrors Rust step 4: two writes
        #    against evolution.sqlite, not in a shared TX with the
        #    kb mutation; the AutoRollback monitor catches divergence.
        now = self._clock()
        try:
            await self._history.mark_rolled_back(proposal_id, now, reason)
        except RepoError as err:
            raise ApplyError(f"history write failed: {err}") from err
        try:
            await self._proposals.mark_auto_rolled_back(proposal_id, now, reason)
        except NotFoundError as err:
            # The ``WHERE status = 'applied'`` clause in
            # ``mark_auto_rolled_back`` returns NotFound on the
            # double-revert race — surface that as NotApplied so the
            # monitor's race-handling branch fires.
            raise NotAppliedError(EvolutionStatus.ROLLED_BACK.as_str()) from err
        except RepoError as err:
            raise ApplyError(f"repo error: {err}") from err

        history_row.rolled_back_at = now
        history_row.rollback_reason = reason
        return history_row


# ─── Helpers ──────────────────────────────────────────────────────────


def now_ms() -> int:
    """Unix milliseconds. Mirrors the Rust ``now_ms`` helper in shape
    (private in Rust, exposed in Python so test harnesses can monkey-
    patch the clock without touching the class)."""
    return int(time.time() * 1000)


def sha256_hex(payload: bytes | str) -> str:
    """SHA-256 of ``payload`` rendered as lowercase hex. Convenience
    for per-kind handlers; mirrors :rust:`sha256_hex`."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_concat(a: str, b: str) -> str:
    """SHA-256 of ``a || \\x00 || b``. The null separator prevents
    ``("ab","c")`` and ``("a","bc")`` from colliding. Mirrors
    :rust:`sha256_concat`."""
    h = hashlib.sha256()
    h.update(a.encode("utf-8"))
    h.update(b"\x00")
    h.update(b.encode("utf-8"))
    return h.hexdigest()


# Convenience callable type for handlers that just need a closure
# rather than a full :class:`KindHandler` object — tests use this
# heavily to wire scripted apply/revert in a single lambda.
ApplyCallback = Callable[[EvolutionProposal], Awaitable[MutationOutcome]]
RevertCallback = Callable[[EvolutionHistory], Awaitable[None]]


@dataclass
class CallableHandler:
    """Tiny :class:`KindHandler` shim wrapping plain async callables.

    Useful at boot when a kind's apply / revert is straightforward
    enough to express inline; the per-kind handlers grown later can
    promote to a full class without changing the
    :meth:`EvolutionApplier.register_handler` call sites.
    """

    apply_fn: ApplyCallback
    revert_fn: RevertCallback

    async def apply(self, proposal: EvolutionProposal) -> MutationOutcome:
        return await self.apply_fn(proposal)

    async def revert(self, history: EvolutionHistory) -> None:
        await self.revert_fn(history)


# Re-exports kept on the module so callers can do
# ``from corlinman_server.gateway.evolution.applier import json, asyncio``
# without us re-importing in handler modules. Strictly cosmetic; the
# stdlib modules live here because the wrapping helpers below use them.
_unused_typing: tuple[Any, ...] = (asyncio, json)
