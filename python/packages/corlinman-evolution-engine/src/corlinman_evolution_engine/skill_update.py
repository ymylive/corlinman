"""Generate ``skill_update`` proposals from skill-invocation failure clusters.

Phase 3-2B Step 1: when a skill (e.g. ``web_search``) repeatedly fails the
observer emits ``skill.invocation.failed`` signals carrying the skill name
as ``target``. A cluster of those on the same skill is enough to flag the
skill card for review.

The diff is intentionally minimal here: we append a single
``<!-- evolution-YYYY-MM-DD: ... -->`` HTML comment to the bottom of the
skill file noting the failure pattern. This proves the diff plumbing without
asking the engine to author prose — that's closed-loop 3-2A territory. The
applier (Step 2) applies the diff verbatim against the real file and the
simulator (Step 3) runs it on a tempdir copy first.

Per the ``KindHandler`` contract this handler is pure data → data: it does
NOT read the actual skill file. The diff is generated symbolically from the
signal cluster and the applier validates against the real file at apply
time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from corlinman_evolution_engine.clustering import SignalCluster
from corlinman_evolution_engine.proposals import EvolutionProposal, ProposalContext
from corlinman_evolution_engine.store import fetch_existing_targets

if TYPE_CHECKING:
    import aiosqlite

KIND_SKILL_UPDATE = "skill_update"

# Trigger event_kind. The observer emits this with ``target=<skill_name>``
# (e.g. ``web_search``) when a skill invocation returns an error result.
EVENT_SKILL_INVOCATION_FAILED = "skill.invocation.failed"


def _skill_path(skill_name: str) -> str:
    """File path target for a skill-update proposal.

    Format: ``skills/<name>.md``. Path is relative to the data dir's
    ``skills/`` root; the applier joins with ``data_dir`` at apply time.
    """
    return f"skills/{skill_name}.md"


def _format_iso_date(now_ms: int) -> str:
    """ISO date used inside the evolution marker comment."""
    return datetime.fromtimestamp(now_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d")


def _build_diff(skill_name: str, cluster: SignalCluster, now_ms: int) -> str:
    """A unified-diff snippet that appends one evolution marker.

    The ``@@`` hunk header uses ``__APPEND__,0`` as a sentinel — the applier
    treats it as "append at EOF" and doesn't attempt context-line matching.
    Step 2 will replace the sentinel once we know the live file's line count
    at apply time.
    """
    iso = _format_iso_date(now_ms)
    skill_file = _skill_path(skill_name)
    note = (
        f"<!-- evolution-{iso}: {cluster.size} failures on skill "
        f"{skill_name!r}; review skill guidance. -->"
    )
    return (
        f"--- a/{skill_file}\n"
        f"+++ b/{skill_file}\n"
        f"@@ __APPEND__,0 +__APPEND__,2 @@\n"
        f"+\n"
        f"+{note}\n"
    )


def _reasoning_for(cluster: SignalCluster) -> str:
    """Human-readable ``reasoning`` for a skill-update proposal."""
    return (
        f"skill invocation failures: {cluster.size} signals on "
        f"skill {cluster.target!r} suggest the skill card needs review"
    )


class SkillUpdateHandler:
    """``KindHandler`` for the ``skill_update`` kind.

    Step 1 implementation: scan the engine's already-clustered signals for
    ``skill.invocation.failed`` clusters and emit one append-only-marker
    proposal per skill. budget_cost=2 because skill content edits are
    higher-impact than tag plumbing — operator should see them surface up
    to the weekly cap faster.

    ``existing_targets`` mirrors MemoryOpHandler so re-running the engine on
    the same day with the same signals does NOT produce duplicate proposals.
    """

    @property
    def kind(self) -> str:
        return KIND_SKILL_UPDATE

    async def existing_targets(self, conn: object) -> set[tuple[str, str]]:
        sqlite_conn: aiosqlite.Connection = conn  # type: ignore[assignment]
        return await fetch_existing_targets(sqlite_conn, self.kind)

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        relevant = [
            c
            for c in ctx.clusters
            if c.event_kind == EVENT_SKILL_INVOCATION_FAILED and c.target
        ]
        if not relevant:
            return []

        # Strongest-signal-first so max_proposals_per_run truncates the tail.
        relevant.sort(key=lambda c: c.size, reverse=True)

        return [
            EvolutionProposal(
                kind=self.kind,
                target=_skill_path(cluster.target or ""),
                diff=_build_diff(cluster.target or "", cluster, ctx.now_ms),
                reasoning=_reasoning_for(cluster),
                risk="medium",
                budget_cost=2,
                signal_ids=cluster.signal_ids,
                trace_ids=cluster.trace_ids,
                tenant_id=cluster.tenant_id,
            )
            for cluster in relevant
        ]
