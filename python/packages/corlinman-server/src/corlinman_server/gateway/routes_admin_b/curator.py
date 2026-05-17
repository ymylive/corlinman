"""``/admin/curator*`` — Wave 4.6 curator UI surface.

End-to-end backend for the new evolution / curator page in the admin UI.
Lets operators:

* preview the curator's deterministic lifecycle pass (dry-run)
* run the pass for real (persists transitions to SKILL.md + curator_state)
* pause / resume the per-profile curator loop
* tune the three thresholds (interval / stale / archive)
* list skills with state + origin + pin badges, filterable
* pin / unpin individual skills

All routes mount behind :func:`require_admin` and gate on three handles
on :class:`AdminState`:

* :attr:`AdminState.profile_store` — confirms the profile exists; 404
  ``profile_not_found`` otherwise.
* :attr:`AdminState.curator_state_repo` — the async
  :class:`corlinman_evolution_store.CuratorStateRepo`. Missing → 503
  ``curator_state_repo_missing``.
* :attr:`AdminState.skill_registry_factory` — synchronous
  ``(slug) -> SkillRegistry`` callable so each request loads a fresh
  view of the profile's skills. Missing → 503
  ``skill_registry_factory_missing``.

The ``signals_repo`` handle is best-effort: when wired, run/preview emit
the same ``EVENT_*`` rows the scheduler-driven curator does; when not
wired, the routes still succeed and just skip signal emission.

Mirrors the Rust pattern from ``routes_admin_b/evolution.py`` — typed
pydantic v2 request/response shapes, error envelopes via ``HTTPException``
detail dicts, deferred imports of the optional store packages so a
partially-installed gateway still imports this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SkillCountsOut(BaseModel):
    """Per-profile skill state histogram returned alongside the curator
    state. ``total`` is the sum across the three lifecycle buckets so the
    UI doesn't have to recompute."""

    active: int = 0
    stale: int = 0
    archived: int = 0
    total: int = 0


class OriginCountsOut(BaseModel):
    """Per-profile origin histogram. Same shape as :class:`SkillCountsOut`
    but bucketed by provenance instead of lifecycle state."""

    bundled: int = 0
    user_requested: int = Field(default=0, alias="user-requested")
    agent_created: int = Field(default=0, alias="agent-created")

    model_config = {"populate_by_name": True}


class ProfileCuratorOut(BaseModel):
    """One row in ``GET /admin/curator/profiles``. Mirrors the
    :class:`corlinman_evolution_store.CuratorState` projection plus the
    skill / origin histograms the UI renders as pills under each profile
    card."""

    slug: str
    paused: bool
    interval_hours: int
    stale_after_days: int
    archive_after_days: int
    last_review_at: str | None = None
    last_review_summary: str | None = None
    run_count: int = 0
    skill_counts: SkillCountsOut = Field(default_factory=SkillCountsOut)
    origin_counts: OriginCountsOut = Field(default_factory=OriginCountsOut)


class CuratorProfilesResponse(BaseModel):
    profiles: list[ProfileCuratorOut]


class TransitionOut(BaseModel):
    """One transition in a :class:`CuratorReport`. Used by both
    preview and real run responses."""

    skill_name: str
    from_state: str
    to_state: str
    reason: str
    days_idle: float


class CuratorReportOut(BaseModel):
    """JSON projection of :class:`gateway.evolution.CuratorReport`.

    Both ``/preview`` and ``/run`` return this exact shape — the only
    difference is whether ``dry_run`` was ``True`` and whether the
    underlying SKILL.md state field was persisted to disk."""

    profile_slug: str
    started_at: str
    finished_at: str
    duration_ms: int
    transitions: list[TransitionOut]
    marked_stale: int
    archived: int
    reactivated: int
    checked: int
    skipped: int


class PauseBody(BaseModel):
    paused: bool


class ThresholdsPatchBody(BaseModel):
    """``PATCH /admin/curator/{slug}/thresholds`` body — every field
    optional so the UI can ship one slider at a time. Validation lives in
    the handler because the cross-field rule (``archive > stale``) doesn't
    fit a single ``Field`` constraint."""

    interval_hours: int | None = Field(default=None, ge=1)
    stale_after_days: int | None = Field(default=None, ge=1)
    archive_after_days: int | None = Field(default=None, ge=1)


class CuratorStateOut(BaseModel):
    """Projection of :class:`CuratorState` used by /pause + /thresholds
    responses. Subset of :class:`ProfileCuratorOut` minus the counts."""

    slug: str
    paused: bool
    interval_hours: int
    stale_after_days: int
    archive_after_days: int
    last_review_at: str | None = None
    last_review_summary: str | None = None
    run_count: int = 0


class SkillSummaryOut(BaseModel):
    """One row in ``GET /admin/curator/{slug}/skills``. Compact shape
    that carries everything the badge-driven list view needs without a
    second fetch."""

    name: str
    description: str
    version: str
    state: str
    origin: str
    pinned: bool
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None


class SkillsListResponse(BaseModel):
    skills: list[SkillSummaryOut]


class PinBody(BaseModel):
    pinned: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    """Render a ``datetime`` as ISO-8601 UTC. ``None`` passes through.

    Mirrors the convention the rest of the admin surface uses (the
    profiles route, evolution history, etc): timezone-aware UTC with a
    ``Z`` suffix, no microseconds. Naive datetimes are assumed UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Drop subsecond precision for stable readable strings.
    return dt.astimezone(timezone.utc).isoformat()


def _profile_store(state: AdminState):
    """Return the wired profile store or raise 503. Mirrors the same
    helper in routes_admin_a/profiles.py — kept private here so the
    routes can fail fast with a single readable envelope."""
    store = getattr(state, "profile_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "profile_store_missing"},
        )
    return store


def _curator_repo(state: AdminState):
    repo = getattr(state, "curator_state_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "curator_state_repo_missing"},
        )
    return repo


def _registry_factory(state: AdminState):
    fn = getattr(state, "skill_registry_factory", None)
    if fn is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "skill_registry_factory_missing"},
        )
    return fn


def _ensure_profile(store, slug: str) -> None:
    """Look up ``slug`` on ``store``; raise 404 if missing. Accepts any
    object that exposes a ``.get(slug)`` returning ``None`` for absent
    rows (matches :class:`ProfileStore` and the in-test fakes)."""
    if store.get(slug) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "profile_not_found", "slug": slug},
        )


def _load_registry(state: AdminState, slug: str):
    """Resolve the skill registry for ``slug`` using the factory. Wraps
    any exception thrown by the factory into a 500 ``registry_load_failed``
    envelope so a malformed skills dir doesn't bubble as a raw 500."""
    factory = _registry_factory(state)
    try:
        return factory(slug)
    except Exception as exc:  # noqa: BLE001 — typed envelope below
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "registry_load_failed",
                "slug": slug,
                "message": str(exc),
            },
        ) from exc


def _report_to_out(report: Any) -> CuratorReportOut:
    """Project a :class:`gateway.evolution.CuratorReport` onto the wire
    envelope. Pulls fields via ``getattr`` so a future struct change that
    adds optional fields doesn't break this projection."""
    transitions = [
        TransitionOut(
            skill_name=t.skill_name,
            from_state=t.from_state,
            to_state=t.to_state,
            reason=t.reason,
            days_idle=float(t.days_idle),
        )
        for t in getattr(report, "transitions", []) or []
    ]
    return CuratorReportOut(
        profile_slug=str(getattr(report, "profile_slug", "")),
        started_at=_iso(getattr(report, "started_at", None)) or "",
        finished_at=_iso(getattr(report, "finished_at", None)) or "",
        duration_ms=int(getattr(report, "duration_ms", 0)),
        transitions=transitions,
        marked_stale=int(getattr(report, "marked_stale", 0)),
        archived=int(getattr(report, "archived", 0)),
        reactivated=int(getattr(report, "reactivated", 0)),
        checked=int(getattr(report, "checked", 0)),
        skipped=int(getattr(report, "skipped", 0)),
    )


def _state_to_out(state_row: Any) -> CuratorStateOut:
    """Slim projection of :class:`CuratorState` for /pause + /thresholds
    responses."""
    return CuratorStateOut(
        slug=str(state_row.profile_slug),
        paused=bool(state_row.paused),
        interval_hours=int(state_row.interval_hours),
        stale_after_days=int(state_row.stale_after_days),
        archive_after_days=int(state_row.archive_after_days),
        last_review_at=_iso(state_row.last_review_at),
        last_review_summary=state_row.last_review_summary,
        run_count=int(state_row.run_count),
    )


def _count_skills(registry: Any) -> tuple[SkillCountsOut, OriginCountsOut]:
    """Walk a :class:`SkillRegistry` once and return both the lifecycle
    state histogram and the origin histogram. Done in one pass so the
    /profiles route never iterates the registry twice."""
    states = SkillCountsOut()
    origins = OriginCountsOut()
    for skill in registry:
        s = getattr(skill, "state", "active")
        if s == "active":
            states.active += 1
        elif s == "stale":
            states.stale += 1
        elif s == "archived":
            states.archived += 1
        states.total += 1
        o = getattr(skill, "origin", "user-requested")
        if o == "bundled":
            origins.bundled += 1
        elif o == "user-requested":
            origins.user_requested += 1
        elif o == "agent-created":
            origins.agent_created += 1
    return states, origins


def _all_profile_slugs(store) -> list[str]:
    """Return every profile slug the store knows about, sorted."""
    profiles = store.list()
    return sorted(str(p.slug) for p in profiles)


async def _run_curator_now(
    *,
    state: AdminState,
    slug: str,
    dry_run: bool,
) -> CuratorReportOut:
    """Shared body for /preview + /run. ``dry_run`` selects the mode.

    Imports :func:`maybe_run_curator` lazily so this module stays
    importable when the curator package isn't installed (the parent agent
    runs evolution-store + skills-registry as separate package boundaries
    — a partial install must still expose a typed 503).
    """
    store = _profile_store(state)
    _ensure_profile(store, slug)
    curator_repo = _curator_repo(state)
    signals_repo = getattr(state, "signals_repo", None)
    if signals_repo is None:
        # The pure logic still works with a no-op signals sink. Build a
        # tiny in-process stub so we don't have to thread an optional
        # parameter through the engine's helper.
        signals_repo = _NoopSignalsRepo()

    try:
        from corlinman_server.gateway.evolution import (  # noqa: PLC0415
            maybe_run_curator,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "curator_module_missing",
                "message": str(exc),
            },
        ) from exc

    registry = _load_registry(state, slug)
    try:
        report = await maybe_run_curator(
            profile_slug=slug,
            registry=registry,
            curator_repo=curator_repo,
            signals_repo=signals_repo,
            force=True,  # the UI invocation always forces a pass
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — typed envelope below
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "curator_run_failed",
                "slug": slug,
                "message": str(exc),
            },
        ) from exc

    if report is None:
        # With ``force=True`` and ``paused=False`` we always get a
        # report; the only ``None`` branch is ``paused=True``. The UI
        # surfaces this as a separate state so the operator knows the
        # action was a no-op rather than a silent failure.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "curator_paused", "slug": slug},
        )

    return _report_to_out(report)


class _NoopSignalsRepo:
    """Drop-in stand-in for :class:`corlinman_evolution_store.SignalsRepo`
    used when the gateway hasn't wired the real repo yet. The curator
    only calls ``insert(signal)``; everything else returns sensibly.

    Kept inside this module so the routes_admin_b package doesn't grow a
    public stub class — this is purely a route-level convenience."""

    async def insert(self, signal: Any) -> int:  # noqa: ARG002
        return 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:  # noqa: C901 — single APIRouter factory, mirrors siblings
    """Sub-router for ``/admin/curator*``. Mounted by
    :func:`corlinman_server.gateway.routes_admin_b.build_router`."""
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "curator"]
    )

    @r.get("/admin/curator/profiles", response_model=CuratorProfilesResponse)
    async def list_profiles(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorProfilesResponse:
        """One row per profile — curator state + skill/origin histograms.

        Walks the ProfileStore for the slug list, then for each slug
        fetches the CuratorState + loads the registry to count skills.
        The histogram pass is O(n) per profile so even a few hundred
        skills stays cheap; the UI polls this every few seconds so the
        cost matters.
        """
        store = _profile_store(admin_state)
        curator_repo = _curator_repo(admin_state)

        slugs = _all_profile_slugs(store)
        rows: list[ProfileCuratorOut] = []
        for slug in slugs:
            state_row = await curator_repo.get(slug)
            # Registry load is best-effort — if a profile has no skills
            # dir yet we still want to surface its curator state.
            try:
                registry = _load_registry(admin_state, slug)
                skill_counts, origin_counts = _count_skills(registry)
            except HTTPException:
                # Re-raise — the factory missing is a hard 503.
                raise
            except Exception:  # noqa: BLE001 — surface empty counts
                skill_counts = SkillCountsOut()
                origin_counts = OriginCountsOut()
            rows.append(
                ProfileCuratorOut(
                    slug=slug,
                    paused=bool(state_row.paused),
                    interval_hours=int(state_row.interval_hours),
                    stale_after_days=int(state_row.stale_after_days),
                    archive_after_days=int(state_row.archive_after_days),
                    last_review_at=_iso(state_row.last_review_at),
                    last_review_summary=state_row.last_review_summary,
                    run_count=int(state_row.run_count),
                    skill_counts=skill_counts,
                    origin_counts=origin_counts,
                )
            )
        return CuratorProfilesResponse(profiles=rows)

    @r.post(
        "/admin/curator/{slug}/preview",
        response_model=CuratorReportOut,
    )
    async def preview_curator_run(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorReportOut:
        """Dry-run: returns the would-be transitions without writing back
        to disk or bumping ``curator_state.last_review_at``. Force-runs
        regardless of the interval window so the UI's "Preview" button
        always returns a meaningful payload."""
        return await _run_curator_now(
            state=admin_state, slug=slug, dry_run=True
        )

    @r.post(
        "/admin/curator/{slug}/run",
        response_model=CuratorReportOut,
    )
    async def run_curator_now(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorReportOut:
        """Real run: persists each transition to SKILL.md, emits signals,
        and bumps ``curator_state.last_review_at`` so the next scheduled
        pass starts from this run. Same envelope as /preview."""
        return await _run_curator_now(
            state=admin_state, slug=slug, dry_run=False
        )

    @r.post(
        "/admin/curator/{slug}/pause",
        response_model=CuratorStateOut,
    )
    async def pause_curator(
        slug: str,
        body: PauseBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorStateOut:
        """Flip the per-profile pause flag. The flag short-circuits
        :func:`maybe_run_curator` *before* any signal emission, so a
        paused profile costs zero work even on the scheduler tick.

        Returns the post-update :class:`CuratorState` so the UI doesn't
        have to refetch."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        curator_repo = _curator_repo(admin_state)

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                CuratorState,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "evolution_store_missing",
                    "message": str(exc),
                },
            ) from exc

        existing = await curator_repo.get(slug)
        updated = CuratorState(
            profile_slug=existing.profile_slug,
            last_review_at=existing.last_review_at,
            last_review_duration_ms=existing.last_review_duration_ms,
            last_review_summary=existing.last_review_summary,
            run_count=existing.run_count,
            paused=bool(body.paused),
            interval_hours=existing.interval_hours,
            stale_after_days=existing.stale_after_days,
            archive_after_days=existing.archive_after_days,
            tenant_id=existing.tenant_id,
        )
        await curator_repo.upsert(updated)
        return _state_to_out(updated)

    @r.patch(
        "/admin/curator/{slug}/thresholds",
        response_model=CuratorStateOut,
    )
    async def update_thresholds(
        slug: str,
        body: ThresholdsPatchBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorStateOut:
        """Update any subset of the three thresholds. Validation:

        * ``interval_hours`` ≥ 1 (handled by pydantic ``Field(ge=1)``)
        * ``stale_after_days`` ≥ 1
        * ``archive_after_days`` > the effective stale threshold

        The cross-field rule uses the *effective* values (incoming
        override stacked on top of the existing row), so a PATCH that
        only changes ``archive_after_days`` is still checked against the
        currently-persisted ``stale_after_days``."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        curator_repo = _curator_repo(admin_state)

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                CuratorState,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "evolution_store_missing",
                    "message": str(exc),
                },
            ) from exc

        existing = await curator_repo.get(slug)
        next_interval = (
            body.interval_hours
            if body.interval_hours is not None
            else existing.interval_hours
        )
        next_stale = (
            body.stale_after_days
            if body.stale_after_days is not None
            else existing.stale_after_days
        )
        next_archive = (
            body.archive_after_days
            if body.archive_after_days is not None
            else existing.archive_after_days
        )

        if next_archive <= next_stale:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_thresholds",
                    "message": (
                        "archive_after_days must be greater than "
                        "stale_after_days"
                    ),
                    "stale_after_days": int(next_stale),
                    "archive_after_days": int(next_archive),
                },
            )

        updated = CuratorState(
            profile_slug=existing.profile_slug,
            last_review_at=existing.last_review_at,
            last_review_duration_ms=existing.last_review_duration_ms,
            last_review_summary=existing.last_review_summary,
            run_count=existing.run_count,
            paused=existing.paused,
            interval_hours=int(next_interval),
            stale_after_days=int(next_stale),
            archive_after_days=int(next_archive),
            tenant_id=existing.tenant_id,
        )
        await curator_repo.upsert(updated)
        return _state_to_out(updated)

    @r.get(
        "/admin/curator/{slug}/skills",
        response_model=SkillsListResponse,
    )
    async def list_profile_skills(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        state_filter: Annotated[
            Literal["active", "stale", "archived"] | None,
            Query(alias="state"),
        ] = None,
        origin_filter: Annotated[
            Literal["bundled", "user-requested", "agent-created"] | None,
            Query(alias="origin"),
        ] = None,
        search: Annotated[str | None, Query()] = None,
    ) -> SkillsListResponse:
        """List every skill for ``slug`` with the badge metadata the UI
        needs in one round trip.

        Filters compose: state and origin are exact-match; ``search`` is
        a case-insensitive substring on ``name`` + ``description`` so an
        operator can type "code" and surface every skill with that
        substring across either field."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        registry = _load_registry(admin_state, slug)

        needle = (search or "").strip().lower()
        rows: list[SkillSummaryOut] = []
        for skill in registry:
            if state_filter is not None and skill.state != state_filter:
                continue
            if origin_filter is not None and skill.origin != origin_filter:
                continue
            if needle:
                hay = f"{skill.name}\n{skill.description}".lower()
                if needle not in hay:
                    continue
            usage = _registry_usage(registry, skill.name)
            rows.append(
                SkillSummaryOut(
                    name=str(skill.name),
                    description=str(skill.description),
                    version=str(getattr(skill, "version", "1.0.0")),
                    state=str(skill.state),
                    origin=str(skill.origin),
                    pinned=bool(skill.pinned),
                    use_count=int(usage.use_count if usage else 0),
                    last_used_at=_iso(
                        usage.last_used_at if usage else None
                    ),
                    created_at=_iso(getattr(skill, "created_at", None)),
                )
            )
        rows.sort(key=lambda row: row.name)
        return SkillsListResponse(skills=rows)

    @r.post(
        "/admin/curator/{slug}/skills/{name}/pin",
        response_model=SkillSummaryOut,
    )
    async def pin_skill(
        slug: str,
        name: str,
        body: PinBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SkillSummaryOut:
        """Toggle :attr:`Skill.pinned` for one skill in ``slug``'s
        registry. Round-trips the new value back to SKILL.md so the
        next registry load picks it up — without this writeback the pin
        would silently revert on every gateway restart.

        Returns the post-update summary (matches /skills row shape)."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        registry = _load_registry(admin_state, slug)

        skill = registry.get(name)
        if skill is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "skill_not_found",
                    "slug": slug,
                    "skill": name,
                },
            )

        skill.pinned = bool(body.pinned)
        try:
            from corlinman_skills_registry import write_skill_md  # noqa: PLC0415
            from corlinman_skills_registry.parse import (  # noqa: PLC0415
                split_frontmatter,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "skills_registry_missing",
                    "message": str(exc),
                },
            ) from exc

        # Preserve the markdown body verbatim — re-read it off disk so we
        # don't accidentally drop edits made by a sibling writer between
        # registry load and this write. Same approach the curator's
        # transition writeback uses.
        source = skill.source_path
        try:
            raw = source.read_text(encoding="utf-8")
            split = split_frontmatter(raw)
            body_md = split[1] if split is not None else raw
            write_skill_md(source, skill, body_md)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "skill_write_failed",
                    "slug": slug,
                    "skill": name,
                    "message": str(exc),
                },
            ) from exc

        usage = _registry_usage(registry, skill.name)
        return SkillSummaryOut(
            name=str(skill.name),
            description=str(skill.description),
            version=str(getattr(skill, "version", "1.0.0")),
            state=str(skill.state),
            origin=str(skill.origin),
            pinned=bool(skill.pinned),
            use_count=int(usage.use_count if usage else 0),
            last_used_at=_iso(usage.last_used_at if usage else None),
            created_at=_iso(getattr(skill, "created_at", None)),
        )

    return r


def _registry_usage(registry: Any, skill_name: str):
    """Pull the :class:`SkillUsage` sidecar for one skill. Tolerates
    registries that don't expose :meth:`usage_for` (the in-test fake) by
    returning ``None`` — the caller already guards against ``None``."""
    fn = getattr(registry, "usage_for", None)
    if fn is None:
        return None
    try:
        return fn(skill_name)
    except Exception:  # noqa: BLE001 — sidecar reads must not raise
        return None
