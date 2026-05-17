"""Apply a user-correction signal by spawning a background-review fork.

Listens for newly-inserted :data:`EVENT_USER_CORRECTION` signals and
routes them to :func:`spawn_background_review` with
``kind="user-correction"``. Rate-limited per ``(profile, session)``
so a chatty user doesn't flood the LLM with consecutive review forks.

This complements (does not replace) the existing
:class:`EvolutionApplier`. We keep the routing logic in its own module
so it can be enabled/disabled independently via config without touching
the main applier. The signal-listener in
:mod:`.signals.user_correction` constructs one instance per gateway
boot and passes :meth:`UserCorrectionApplier.apply` as its
``on_signal`` callback.

Failure mode
------------

:meth:`UserCorrectionApplier.apply` returns ``None`` when:

* The signal is not a user-correction (defensive guard).
* The signal's payload weight is below ``min_weight``.
* The per-session rate-limit hasn't elapsed.
* The signal can't be resolved to a profile/registry/provider.

Otherwise it returns the :class:`BackgroundReviewReport` produced by
:func:`spawn_background_review`. That call is itself exception-safe —
it never raises — so :meth:`apply` returns a report even on provider
failures, with the report's ``error`` field populated.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog
from corlinman_evolution_store import EVENT_USER_CORRECTION, EvolutionSignal

from corlinman_server.gateway.evolution.background_review import (
    BackgroundReviewReport,
    spawn_background_review,
)

logger = structlog.get_logger(__name__)

__all__ = ["UserCorrectionApplier"]


# Type aliases — kept module-level so callers can spell the resolver
# shapes in their own type hints without re-importing.
RegistryResolver = Callable[[str], Any]                       # profile_slug -> SkillRegistry
ProfileRootResolver = Callable[[str], Path]                   # profile_slug -> profile_root path
ProviderResolver = Callable[[str], tuple[Any, str]]           # profile_slug -> (provider, model)
SpawnFn = Callable[..., Awaitable[BackgroundReviewReport]]


def _utc_now() -> datetime:
    """Module-private clock; replaced in tests via dataclass injection."""
    return datetime.now(timezone.utc)


class UserCorrectionApplier:
    """Stateful rate-limiter + dispatcher for user-correction signals.

    Construct one per gateway and pass :meth:`apply` to the signal
    listener as its ``on_signal`` callback. The applier holds an
    in-memory ``(profile, session) -> last_fire_at`` map so consecutive
    corrections in the same session collapse to a single review fork.

    Parameters
    ----------
    registry_for_profile:
        Resolver from ``profile_slug`` to a live
        :class:`corlinman_skills_registry.SkillRegistry`. The applier
        passes the registry through to the background review without
        inspecting it — failures inside the resolver short-circuit the
        call.
    profile_root_for_profile:
        Resolver from ``profile_slug`` to the on-disk profile root
        (the directory that holds ``skills/``, ``MEMORY.md``, …).
    provider_for_profile:
        Resolver from ``profile_slug`` to ``(provider, model)`` — the
        same pair the chat path uses. Re-using the user's primary
        provider keeps the background review cost-attributed correctly.
    rate_limit_seconds:
        Minimum wall-clock interval between two fires for the same
        ``(profile, session)`` tuple. Default 30s mirrors hermes-agent's
        cooldown.
    min_weight:
        Floor on the detector's confidence (the ``weight`` field in
        the signal payload). Defaults to ``0.7`` so the weakest match
        kind ("reformulation", weight 0.55) is suppressed by default.
    timeout_seconds:
        Per-call timeout for the spawned background review. Defaults
        to 30s — user-correction reviews are tighter than the general
        ``combined`` review because the user is actively waiting.
    spawn_fn:
        Injection seam for tests; defaults to the production
        :func:`spawn_background_review`.
    now_fn:
        Injection seam for tests; defaults to :func:`_utc_now`.
    """

    def __init__(
        self,
        *,
        registry_for_profile: RegistryResolver,
        profile_root_for_profile: ProfileRootResolver,
        provider_for_profile: ProviderResolver,
        rate_limit_seconds: int = 30,
        min_weight: float = 0.7,
        timeout_seconds: float = 30.0,
        spawn_fn: SpawnFn | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry_for_profile = registry_for_profile
        self._profile_root_for_profile = profile_root_for_profile
        self._provider_for_profile = provider_for_profile
        self._rate_limit = timedelta(seconds=max(0, int(rate_limit_seconds)))
        self._min_weight = float(min_weight)
        self._timeout_seconds = float(timeout_seconds)
        self._spawn_fn: SpawnFn = spawn_fn or spawn_background_review
        self._now: Callable[[], datetime] = now_fn or _utc_now
        # ``(profile_slug, session_id_or_global) -> last fire timestamp``.
        # We never prune this map — even a year of unique sessions is
        # < 1 MB. The gateway restarts before that becomes a problem.
        self._last_fired: dict[tuple[str, str], datetime] = {}

    # ─── Public API ──────────────────────────────────────────────

    async def apply(self, signal: EvolutionSignal) -> BackgroundReviewReport | None:
        """Spawn a background review for ``signal`` if all gates pass.

        Returns ``None`` if gated; otherwise the
        :class:`BackgroundReviewReport` from :func:`spawn_background_review`.
        Never raises — provider/resolver failures surface as logged
        warnings or, in the case of provider failure, as a report whose
        ``error`` field is populated.
        """
        log = logger.bind(
            signal_id=signal.id,
            event_kind=signal.event_kind,
            target=signal.target,
            session_id=signal.session_id,
        )

        # 1. Defensive event_kind gate. Listeners *should* only hand us
        #    USER_CORRECTION signals but we double-check so an accidental
        #    wire-up never spawns a review for unrelated signal kinds.
        if signal.event_kind != EVENT_USER_CORRECTION:
            log.debug("user_correction_applier.skipped reason=wrong_event_kind")
            return None

        # 2. Parse the payload. The detector populates ``weight`` + ``text``
        #    + ``kind``; defensive defaults keep us alive if a malformed
        #    payload sneaks in from a future detector revision.
        payload = signal.payload_json or {}
        if not isinstance(payload, dict):
            log.debug("user_correction_applier.skipped reason=payload_not_dict")
            return None
        weight = payload.get("weight")
        text = payload.get("text") or ""
        kind = payload.get("kind") or "unknown"

        try:
            weight_f = float(weight)
        except (TypeError, ValueError):
            log.debug("user_correction_applier.skipped reason=missing_weight")
            return None

        if weight_f < self._min_weight:
            log.debug(
                "user_correction_applier.skipped",
                reason="weight_below_min",
                weight=weight_f,
                min_weight=self._min_weight,
            )
            return None

        # 3. Resolve the profile slug. We prefer ``signal.target`` because
        #    the listener stamps it from the gateway's session→skill /
        #    profile context. Fall back to the tenant id so single-tenant
        #    deployments (tenant_id == profile_slug == "default") still
        #    fire.
        profile_slug = signal.target or signal.tenant_id
        if not profile_slug:
            log.debug("user_correction_applier.skipped reason=no_profile_slug")
            return None

        # 4. Rate-limit. ``session_id`` may be ``None`` (e.g. signal came
        #    from a non-session origin); we collapse that into ``"global"``
        #    so a per-profile limiter still applies.
        session_key = signal.session_id or "global"
        rate_key = (profile_slug, session_key)
        now = self._now()
        last = self._last_fired.get(rate_key)
        if last is not None and (now - last) < self._rate_limit:
            log.debug(
                "user_correction_applier.skipped",
                reason="rate_limited",
                seconds_since_last=(now - last).total_seconds(),
                rate_limit_seconds=self._rate_limit.total_seconds(),
            )
            return None

        # 5. Resolve registry + profile_root + provider. Each resolver
        #    may legitimately raise (e.g. profile evicted between insert
        #    and apply). We treat any failure as "gate the review" and
        #    log at debug so the audit trail isn't flooded by transient
        #    misses.
        try:
            registry = self._registry_for_profile(profile_slug)
        except Exception as err:  # noqa: BLE001
            log.debug("user_correction_applier.skipped reason=registry_resolve_failed err=%s", err)
            return None
        try:
            profile_root = self._profile_root_for_profile(profile_slug)
        except Exception as err:  # noqa: BLE001
            log.debug("user_correction_applier.skipped reason=profile_root_resolve_failed err=%s", err)
            return None
        try:
            provider, model = self._provider_for_profile(profile_slug)
        except Exception as err:  # noqa: BLE001
            log.debug("user_correction_applier.skipped reason=provider_resolve_failed err=%s", err)
            return None

        # 6. Mark the rate-limit *before* dispatching — even if the
        #    review fails we don't want to retry on every burst signal.
        self._last_fired[rate_key] = now

        log.info(
            "user_correction_applier.spawn",
            profile_slug=profile_slug,
            kind=kind,
            weight=weight_f,
            session_key=session_key,
        )

        # 7. Dispatch. ``spawn_background_review`` is exception-safe;
        #    we wrap one more layer just in case a future refactor
        #    forgets that contract.
        try:
            report = await self._spawn_fn(
                kind="user-correction",
                user_correction_text=text,
                profile_slug=profile_slug,
                profile_root=Path(profile_root),
                recent_messages=[],
                registry=registry,
                provider=provider,
                model=model,
                timeout_seconds=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — defensive umbrella
            log.warning("user_correction_applier.spawn_failed err=%s", err)
            return None

        log.info(
            "user_correction_applier.completed",
            applied=getattr(report, "applied_count", None),
            skipped=getattr(report, "skipped_count", None),
            error=getattr(report, "error", None),
        )
        return report

    # ─── Helpers for tests ──────────────────────────────────────

    def _reset_rate_limit(self) -> None:
        """Test-only seam — clears the rate-limit memory."""
        self._last_fired.clear()
