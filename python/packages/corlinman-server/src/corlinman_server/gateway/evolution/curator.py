"""Lifecycle curator — deterministic skill state transitions.

Port of hermes-agent's pure-logic curator (``agent/curator.py:256-296``).
This module is intentionally LLM-free: the background-review fork that
needs an LLM lives in :mod:`background_review`. The two compose at the
gateway entry point — observer detects idle, this module runs the
deterministic pass, then optionally the background_review fork runs an
LLM-driven consolidation. Both report back via :class:`EvolutionSignal`.

Rules ported from hermes:

* ``state == "active"`` + idle > ``stale_after_days``  → ``"stale"``
* ``state == "stale"``  + idle > ``archive_after_days`` → ``"archived"``
* ``state == "stale"``  + any use (``last_used_at > last_review_at``)
                                                       → ``"active"``
* ``pinned is True`` → skip (operator-pinned, never touch)
* ``origin != "agent-created"`` → skip (curator only manages skills it
  created — see hermes ``tools/skill_usage.py:154-200`` provenance
  filter)

The pure logic core (:func:`apply_lifecycle_transitions`) takes an
explicit ``now`` so time-travel is trivial in tests. The async outer
loop (:func:`maybe_run_curator`) gates on the per-profile
``CuratorState.last_review_at`` + ``interval_hours`` so an idle-trigger
fires at most once per configured window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from corlinman_evolution_store import (
    EVENT_CURATOR_RUN_COMPLETED,
    EVENT_CURATOR_RUN_FAILED,
    EVENT_IDLE_REFLECTION,
    EVENT_SKILL_UNUSED,
    CuratorState,
    CuratorStateRepo,
    EvolutionSignal,
    SignalsRepo,
    SignalSeverity,
)
from corlinman_skills_registry import Skill, SkillRegistry, write_skill_md
from corlinman_skills_registry.usage import SkillUsage

__all__ = [
    "CuratorReport",
    "CuratorTransition",
    "apply_lifecycle_transitions",
    "maybe_run_curator",
]


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratorTransition:
    """One state change applied (or proposed, in dry-run) by
    :func:`apply_lifecycle_transitions`.

    The ``reason`` field is one of ``"stale_threshold"`` /
    ``"archive_threshold"`` / ``"reactivated"`` — surfaced verbatim in
    the ``EVENT_SKILL_UNUSED`` signal payload so the admin UI can render
    *why* a transition happened without re-deriving thresholds.
    """

    skill_name: str
    from_state: str
    to_state: str
    reason: str
    days_idle: float


@dataclass(frozen=True)
class CuratorReport:
    """Result of a single :func:`maybe_run_curator` invocation.

    ``checked`` counts every skill the curator considered (whether or
    not it transitioned); ``skipped`` counts skills filtered out for
    provenance / pinning reasons. ``checked - skipped`` ≈ the eligible
    pool, but the literal subtraction isn't exact because skills with
    no state change still count toward ``checked``.
    """

    profile_slug: str
    started_at: datetime
    finished_at: datetime
    transitions: list[CuratorTransition]
    skipped: int
    checked: int

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def marked_stale(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "stale")

    @property
    def archived(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "archived")

    @property
    def reactivated(self) -> int:
        return sum(1 for t in self.transitions if t.to_state == "active")

    def summary_line(self) -> str:
        """One-line human summary stored in
        :attr:`CuratorState.last_review_summary`."""
        return (
            f"stale={self.marked_stale} archived={self.archived} "
            f"reactivated={self.reactivated} checked={self.checked} "
            f"skipped={self.skipped} duration={self.duration_ms}ms"
        )


# ---------------------------------------------------------------------------
# Pure logic — `apply_lifecycle_transitions`
# ---------------------------------------------------------------------------


def _days_between(later: datetime, earlier: datetime | None) -> float:
    """Inclusive-friendly day delta. ``None`` earlier → ``inf`` so
    a never-used skill is treated as maximally idle (the caller still
    falls back to ``created_at`` before reaching this branch)."""
    if earlier is None:
        return float("inf")
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    return (later - earlier).total_seconds() / 86400.0


def _classify_transition(
    skill: Skill,
    usage: SkillUsage,
    state_row: CuratorState,
    now: datetime,
) -> CuratorTransition | None:
    """Pure: decide whether ``skill`` needs a state change. ``None`` ==
    no-op.

    Mirrors the hermes ``agent/curator.py:256-296`` cascade, with the
    provenance filter from ``tools/skill_usage.py:154-200`` hoisted to
    the top so non-eligible skills bail before any time math.
    """
    # Provenance / pin guards — must come first so we don't even count
    # the days for skills we'd never touch anyway.
    if skill.pinned:
        return None
    if skill.origin != "agent-created":
        return None

    # Anchor: prefer recorded ``last_used_at``, else fall back to
    # ``created_at`` so a brand-new skill that hasn't been used yet
    # doesn't immediately archive itself.
    last_active = usage.last_used_at if usage.last_used_at is not None else skill.created_at
    days_idle = _days_between(now, last_active)

    # Reactivation: a stale skill got used after the last curator review.
    # We can only assert "after" when we have both timestamps; if
    # ``last_review_at`` is None we'd need a different signal (so we
    # leave the active→stale → stale→archived ladder to fire instead).
    if (
        skill.state == "stale"
        and usage.last_used_at is not None
        and state_row.last_review_at is not None
        and usage.last_used_at > state_row.last_review_at
    ):
        return CuratorTransition(
            skill_name=skill.name,
            from_state="stale",
            to_state="active",
            reason="reactivated",
            days_idle=days_idle,
        )

    # stale → archived (check before active → stale so the longer
    # threshold wins on a skill that crossed both at once).
    if skill.state == "stale" and days_idle > state_row.archive_after_days:
        return CuratorTransition(
            skill_name=skill.name,
            from_state="stale",
            to_state="archived",
            reason="archive_threshold",
            days_idle=days_idle,
        )

    # active → stale
    if skill.state == "active" and days_idle > state_row.stale_after_days:
        return CuratorTransition(
            skill_name=skill.name,
            from_state="active",
            to_state="stale",
            reason="stale_threshold",
            days_idle=days_idle,
        )

    return None


def _split_body(source_path) -> str:
    """Re-read the SKILL.md body so the round-trip write doesn't lose
    handcrafted Markdown when the curator only meant to flip a state
    flag.

    We can't trust :attr:`Skill.body_markdown` once the agent has been
    in memory for a while — a sibling skill writer (W4.4 background
    review) may have rewritten the body on disk without re-syncing the
    in-memory copy. Always pull the body off disk just before write.
    """
    from corlinman_skills_registry.parse import split_frontmatter

    text = source_path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        # File doesn't have frontmatter (shouldn't happen for a
        # registry-loaded skill) — fall back to the whole text as body.
        return text
    _yaml_str, body = split
    return body


def apply_lifecycle_transitions(
    registry: SkillRegistry,
    state_row: CuratorState,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[CuratorTransition]:
    """Run the deterministic pass over every skill in ``registry``.

    With ``dry_run=True`` returns the proposed transitions without
    mutating any SKILL.md on disk (the in-memory :class:`Skill` objects
    are left untouched too — callers can re-classify after writes
    elsewhere). With ``dry_run=False`` (the default) writes back via
    :func:`write_skill_md`, preserving the body verbatim by reading it
    off disk just before the write.

    The pure-logic decision sits in :func:`_classify_transition` —
    this wrapper is the side-effect surface.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    transitions: list[CuratorTransition] = []

    for skill in registry:
        usage = registry.usage_for(skill.name)
        transition = _classify_transition(skill, usage, state_row, when)
        if transition is None:
            continue
        transitions.append(transition)
        if dry_run:
            continue
        # Mutate the in-memory model in place then round-trip the file.
        # ``Skill`` is a pydantic v2 BaseModel with frozen=False so
        # direct attribute assignment is supported.
        skill.state = transition.to_state  # type: ignore[assignment]
        path = registry.path_for(skill.name)
        if path is None:
            # Registry returned a skill whose path can't be resolved —
            # nothing on disk to write back. Keep the in-memory flip
            # so subsequent passes see the new state.
            log.warning(
                "curator.path_missing",
                skill=skill.name,
                to_state=transition.to_state,
            )
            continue
        # ``path_for`` returns the directory; the SKILL.md itself is on
        # ``skill.source_path``.
        body = _split_body(skill.source_path)
        write_skill_md(skill.source_path, skill, body)

    return transitions


# ---------------------------------------------------------------------------
# Idle trigger — `maybe_run_curator`
# ---------------------------------------------------------------------------


def _now_ms(when: datetime) -> int:
    """Unix milliseconds for the signal ``observed_at`` field.

    Tests pass an explicit ``now`` to keep timestamps deterministic;
    we never call :func:`datetime.now` inside the pure path.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


async def _emit_signal(
    signals: SignalsRepo,
    *,
    event_kind: str,
    severity: SignalSeverity,
    target: str | None,
    payload: dict,
    observed_at: int,
    tenant_id: str,
) -> None:
    """Best-effort signal insert. We never let a signal write failure
    prevent the curator from making forward progress on the SKILL.md
    side — the same philosophy the observer applies (see
    ``observer.py:212-217`` ``write_failed`` log)."""
    try:
        await signals.insert(
            EvolutionSignal(
                event_kind=event_kind,
                severity=severity,
                payload_json=payload,
                target=target,
                observed_at=observed_at,
                tenant_id=tenant_id,
            )
        )
    except Exception as err:  # noqa: BLE001 — log + drop
        log.warning(
            "curator.signal_write_failed",
            event_kind=event_kind,
            err=str(err),
        )


async def maybe_run_curator(
    *,
    profile_slug: str,
    registry: SkillRegistry,
    curator_repo: CuratorStateRepo,
    signals_repo: SignalsRepo,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> CuratorReport | None:
    """Check the per-profile interval; if elapsed (or ``force=True``),
    run :func:`apply_lifecycle_transitions` and emit signals.

    Returns ``None`` when the curator decided not to run (paused, or
    inside the interval window). Returns a :class:`CuratorReport`
    otherwise — even on dry-runs, so the caller can render a preview.

    Mirrors hermes ``maybe_run_curator`` / ``should_run_now``
    (``agent/curator.py:198-248``) but folds the "seed first-run state"
    behaviour from there into a single decision: when
    ``last_review_at`` is ``None`` we still run, on the theory that the
    operator just installed the curator and *wants* an immediate first
    pass. The hermes "defer first run by one interval" trick relied on
    ``hermes update`` ticking the loop every minute; corlinman's
    scheduler fires this exact entry point on its own cadence, so we
    don't need that defer.

    Side effects (when we decide to run):

    * Emit :data:`EVENT_IDLE_REFLECTION` before starting (so the admin
      UI sees the trigger even if the transition pass crashes).
    * Run :func:`apply_lifecycle_transitions`; on exception emit
      :data:`EVENT_CURATOR_RUN_FAILED` with ``{"error": str(err)}`` in
      the payload, then re-raise.
    * For each transition emit one :data:`EVENT_SKILL_UNUSED` with
      ``payload={"from": ..., "to": ..., "reason": ...,
      "days_idle": ...}``.
    * Emit :data:`EVENT_CURATOR_RUN_COMPLETED` with the summary line.
    * When ``dry_run=False``, persist via
      :meth:`CuratorStateRepo.mark_run` so the next interval window
      starts from this run.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    state = await curator_repo.get(profile_slug)

    # Paused → do nothing, not even a signal. Matches hermes
    # ``is_paused()`` short-circuit in ``should_run_now`` (curator.py:222).
    if state.paused:
        log.debug("curator.paused", profile_slug=profile_slug)
        return None

    # Interval gate. ``None`` last_review_at means "never run before" —
    # treat as eligible. Otherwise require the configured window has
    # elapsed unless the caller forced us in.
    if not force and state.last_review_at is not None:
        elapsed = when - state.last_review_at
        if elapsed < timedelta(hours=state.interval_hours):
            log.debug(
                "curator.too_soon",
                profile_slug=profile_slug,
                elapsed_hours=elapsed.total_seconds() / 3600.0,
                interval_hours=state.interval_hours,
            )
            return None

    observed_at = _now_ms(when)
    tenant_id = state.tenant_id

    # Emit the trigger signal first — even if the pass crashes mid-run
    # the admin UI can correlate the trigger with the failure event.
    await _emit_signal(
        signals_repo,
        event_kind=EVENT_IDLE_REFLECTION,
        severity=SignalSeverity.INFO,
        target=profile_slug,
        payload={
            "profile_slug": profile_slug,
            "force": force,
            "dry_run": dry_run,
        },
        observed_at=observed_at,
        tenant_id=tenant_id,
    )

    started_at = when
    try:
        transitions = apply_lifecycle_transitions(
            registry,
            state,
            now=when,
            dry_run=dry_run,
        )
    except Exception as err:  # noqa: BLE001 — re-raised below
        await _emit_signal(
            signals_repo,
            event_kind=EVENT_CURATOR_RUN_FAILED,
            severity=SignalSeverity.ERROR,
            target=profile_slug,
            payload={
                "profile_slug": profile_slug,
                "error": str(err),
                "dry_run": dry_run,
            },
            observed_at=_now_ms(when),
            tenant_id=tenant_id,
        )
        raise

    finished_at = when  # pure logic is sync — start ≈ finish at the
    # signal grain. The per-skill writebacks are tiny; if we ever need
    # truer durations we can sample ``time.monotonic()`` around the
    # call, but ``CuratorReport.duration_ms`` would still be 0 on the
    # current clock since ``now`` is fixed.

    # Count every skill we *considered* so the report's ``checked``
    # field matches the hermes ``counts["checked"]`` semantic. The
    # ``skipped`` count is everything that bailed for provenance / pin.
    checked = 0
    skipped = 0
    for skill in registry:
        checked += 1
        if skill.pinned or skill.origin != "agent-created":
            skipped += 1

    report = CuratorReport(
        profile_slug=profile_slug,
        started_at=started_at,
        finished_at=finished_at,
        transitions=transitions,
        skipped=skipped,
        checked=checked,
    )

    # Per-transition signals so the admin UI can render a "what changed"
    # list keyed by skill name without re-reading every SKILL.md.
    for transition in transitions:
        await _emit_signal(
            signals_repo,
            event_kind=EVENT_SKILL_UNUSED,
            severity=SignalSeverity.INFO,
            target=transition.skill_name,
            payload={
                "from": transition.from_state,
                "to": transition.to_state,
                "reason": transition.reason,
                "days_idle": transition.days_idle,
                "profile_slug": profile_slug,
            },
            observed_at=observed_at,
            tenant_id=tenant_id,
        )

    summary = report.summary_line()
    await _emit_signal(
        signals_repo,
        event_kind=EVENT_CURATOR_RUN_COMPLETED,
        severity=SignalSeverity.INFO,
        target=profile_slug,
        payload={
            "profile_slug": profile_slug,
            "summary": summary,
            "marked_stale": report.marked_stale,
            "archived": report.archived,
            "reactivated": report.reactivated,
            "checked": report.checked,
            "skipped": report.skipped,
            "duration_ms": report.duration_ms,
            "dry_run": dry_run,
        },
        observed_at=observed_at,
        tenant_id=tenant_id,
    )

    # Only flip the per-profile ``last_review_at`` on a real run. A
    # dry-run preview shouldn't move the interval window — the operator
    # may want to preview and then apply within the same window.
    if not dry_run:
        await curator_repo.mark_run(
            profile_slug,
            duration_ms=report.duration_ms,
            summary=summary,
            now=when,
            tenant_id=tenant_id,
        )

    return report
