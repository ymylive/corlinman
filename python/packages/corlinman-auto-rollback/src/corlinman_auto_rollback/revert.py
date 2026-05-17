"""``Applier`` protocol — the surface the monitor depends on without
pulling in the gateway / server crate.

Ported 1:1 from ``rust/crates/corlinman-auto-rollback/src/revert.rs``.
The concrete implementation lives elsewhere (the gateway's
``EvolutionApplier``); this module only defines the contract +
typed error set the monitor inspects.

Python uses a runtime-checkable ``Protocol`` instead of an ABC so test
mocks don't have to inherit anything — mirrors the Rust ``async_trait``
trait surface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from corlinman_evolution_store import ProposalId


class RevertError(Exception):
    """Base for every typed failure mode of :meth:`Applier.revert`.

    Subclasses below cover the four cases the Rust ``RevertError`` enum
    distinguishes; concrete appliers (the gateway-side
    ``EvolutionApplier``) map their richer ``ApplyError`` into one of
    these. The monitor switches on the concrete subclass, so adding a
    new variant is intentionally breaking.
    """


class NotFoundRevertError(RevertError):
    """Proposal id wasn't in ``evolution_proposals``."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"proposal not found: {proposal_id}")
        self.proposal_id = proposal_id


class NotAppliedRevertError(RevertError):
    """Proposal exists but isn't in ``applied`` (already rolled back,
    or never made it to apply). Carries the actual status string so
    the monitor can log the race cleanly."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal not applied (status={status})")
        self.status = status


class HistoryMissingRevertError(RevertError):
    """History row missing — the forward apply must have written one,
    so this signals data corruption rather than a routine miss."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"history row missing for proposal {proposal_id}")
        self.proposal_id = proposal_id


class UnsupportedKindRevertError(RevertError):
    """Kind has no revert handler yet (W1-B ships memory_op only)."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"kind {kind} cannot be reverted yet")
        self.kind = kind


class InternalRevertError(RevertError):
    """Anything the gateway couldn't classify above (kb mutation
    failure, malformed inverse_diff, transaction error, ...). The
    monitor logs + skips; an operator inspects the gateway logs."""

    def __init__(self, message: str) -> None:
        super().__init__(f"revert failed: {message}")
        self.message = message


@runtime_checkable
class Applier(Protocol):
    """Thin contract the AutoRollback monitor calls into.

    One method on purpose — the monitor only ever needs "revert this id,
    here's why". Production wires in a concrete implementation
    (gateway-side); tests wire in a mock that records calls.
    """

    async def revert(self, proposal_id: ProposalId, reason: str) -> None:
        """Revert the proposal identified by ``proposal_id``. The
        ``reason`` is persisted into ``evolution_proposals.auto_rollback_reason``
        + ``evolution_history.rollback_reason`` by the implementation.

        Raises one of the :class:`RevertError` subclasses on failure.
        """
        ...


__all__ = [
    "Applier",
    "HistoryMissingRevertError",
    "InternalRevertError",
    "NotAppliedRevertError",
    "NotFoundRevertError",
    "RevertError",
    "UnsupportedKindRevertError",
]
