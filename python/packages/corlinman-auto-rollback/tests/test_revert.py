"""Tests for the :class:`Applier` protocol + :class:`RevertError`
hierarchy. Ports the smoke tests in
``rust/crates/corlinman-auto-rollback/src/revert.rs::tests``.
"""

from __future__ import annotations

import pytest
from corlinman_auto_rollback.revert import (
    Applier,
    HistoryMissingRevertError,
    InternalRevertError,
    NotAppliedRevertError,
    NotFoundRevertError,
    RevertError,
    UnsupportedKindRevertError,
)
from corlinman_evolution_store import ProposalId


class _MockApplier:
    """Records (id, reason); returns whatever ``result`` says.

    Mirrors the shape from the Rust ``revert::tests::MockApplier``.
    Intentionally does NOT inherit from anything — we lean on the
    runtime-checkable :class:`Applier` protocol so a duck-typed class
    satisfies the contract.
    """

    def __init__(self, result: RevertError | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result

    async def revert(self, proposal_id: ProposalId, reason: str) -> None:
        self.calls.append((str(proposal_id), reason))
        if self._result is not None:
            raise self._result


@pytest.mark.asyncio
async def test_applier_trait_records_call_and_returns_ok() -> None:
    m = _MockApplier()
    pid = ProposalId("evol-mock-001")
    await m.revert(pid, "test reason")
    assert m.calls == [("evol-mock-001", "test reason")]


def test_mock_applier_satisfies_protocol() -> None:
    assert isinstance(_MockApplier(), Applier)


@pytest.mark.asyncio
async def test_applier_trait_propagates_each_error_variant() -> None:
    # Smoke each variant so the surface stays exhaustive — adding a
    # new variant means adding it here, which is the point.
    cases: list[RevertError] = [
        NotFoundRevertError("p"),
        NotAppliedRevertError("approved"),
        HistoryMissingRevertError("p"),
        UnsupportedKindRevertError("tag_rebalance"),
        InternalRevertError("kb closed"),
    ]
    for err in cases:
        m = _MockApplier(err)
        pid = ProposalId("evol-mock-002")
        with pytest.raises(type(err)):
            await m.revert(pid, "r")


def test_not_applied_revert_error_exposes_status() -> None:
    err = NotAppliedRevertError("rolled_back")
    assert err.status == "rolled_back"
    assert "rolled_back" in str(err)
