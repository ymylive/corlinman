"""End-to-end distillation runner — wires iter 1-3 primitives.

Per ``docs/design/phase4-w4-d1-design.md`` §"Distillation job", a
single pass over ``[window_start, window_end)`` does:

1. Sweep stale ``running`` rows (crash-resume contract).
2. Pick ``(window_start, window_end)`` via :func:`select_window`,
   clamped to ``latest_ok_run.window_end``.
3. Short-circuit if the window is below ``min_window_secs`` →
   ``status='skipped_empty'``.
4. ``open_run`` — claim the unique-window guard. On collision, return
   the prior run row (idempotent re-run).
5. ``collect_bundles`` — multi-stream join over the window.
6. For each bundle:
     a. ``classify`` → :class:`EpisodeKind`.
     b. ``importance.score`` → frozen importance.
     c. ``distill`` → ``DistilledSummary`` via injected provider.
     d. Natural-key probe — skip if a half-flushed prior run already
        wrote this ``(tenant, started, ended, kind)``.
     e. ``insert_episode`` with ``embedding=NULL``.
7. ``finish_run`` — stamp ``status='ok'`` + ``episodes_written``.

The runner is the iter-4 surface; iter 5 layers the second-pass
embedding writer on top of the same store, and iter 6 wraps a CLI
``distill-once`` entry point + a scheduler-friendly module callable.

The provider for the LLM call is injected (a :class:`SummaryProvider`
or a plain ``async`` callable) so tests stay fully offline. The
production wiring lives in iter 7+ (real ``corlinman-providers``
adapter).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from corlinman_episodes.classifier import classify
from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.distiller import SummaryFn, SummaryProvider, distill
from corlinman_episodes.importance import score
from corlinman_episodes.sources import (
    SourceBundle,
    SourcePaths,
    collect_bundles,
    select_window,
    window_too_small,
)
from corlinman_episodes.store import (
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_SKIPPED_EMPTY,
    DistillationRun,
    Episode,
    EpisodesStore,
    RunWindowConflictError,
    new_episode_id,
)

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single :func:`episodes_run_once` call.

    The runner returns one of these whether the pass minted episodes,
    short-circuited the window, or hit an idempotent re-run. Callers
    (CLI, scheduler, admin route) decide what to log / surface based on
    ``status``. ``run_id`` is always set so the operator can trace.
    """

    tenant_id: str
    run_id: str
    status: str
    window_start_ms: int
    window_end_ms: int
    episodes_written: int = 0
    episodes_reused: int = 0
    bundles_seen: int = 0
    swept_stale_runs: tuple[str, ...] = field(default_factory=tuple)
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        """True for terminal-success statuses (``ok`` / ``skipped_empty``).

        Mirrors :meth:`EpisodesStore.latest_ok_run`'s notion of
        "advanced the window" — both count for the next pass's
        clamp computation.
        """
        return self.status in (RUN_STATUS_OK, RUN_STATUS_SKIPPED_EMPTY)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def episodes_run_once(
    *,
    config: EpisodesConfig,
    episodes_db: Path,
    sources: SourcePaths,
    summary_provider: SummaryProvider | SummaryFn,
    tenant_id: str = DEFAULT_TENANT_ID,
    now_ms: int | None = None,
) -> RunSummary:
    """Run one distillation pass and return its summary.

    The function is the single orchestration seam for D1 — the CLI,
    the admin route, and the scheduler all call into this. Every
    branch (short-circuit, idempotent collision, happy path, partial
    failure) writes a deterministic ``episode_distillation_runs`` row,
    so an operator inspecting the log can always reconstruct what
    happened.

    Caller is responsible for:
      - Opening any LLM provider HTTP client (we inject the callable).
      - Embedding pass — runs as a separate step (iter 5
        :func:`populate_pending_embeddings`) so an embedding outage
        doesn't block summary persistence.

    ``now_ms`` is exposed for tests; production callers leave it None.
    """
    if not config.enabled:
        # Mirrors the ``ConsolidationConfig.enabled=False`` short-circuit
        # — emit a deterministic, no-side-effects skip.
        return _disabled_skip(tenant_id=tenant_id, now_ms=now_ms or _now_ms())

    now = now_ms if now_ms is not None else _now_ms()

    async with EpisodesStore(episodes_db) as store:
        # 1. Sweep stale ``running`` rows so the unique-window guard
        #    doesn't false-collide on a ghost from a crashed prior pass.
        swept = await store.sweep_stale_runs(
            now_ms=now,
            stale_after_secs=config.run_stale_after_secs,
        )
        if swept:
            logger.info(
                "episodes.runner: swept stale runs",
                extra={"tenant_id": tenant_id, "swept": list(swept)},
            )

        # 2. Window selection — clamp to last successful run.
        latest_ok = await store.latest_ok_run(tenant_id=tenant_id)
        window_start, window_end = select_window(
            now_ms=now,
            distillation_window_hours=config.distillation_window_hours,
            last_ok_run_window_end_ms=(
                latest_ok.window_end if latest_ok is not None else None
            ),
        )

        # 3. Short-circuit on a too-small window — a back-to-back cron
        #    tick or a clamp that ate most of the rolling start would
        #    leave nothing to distill. Stamp a skipped_empty row so the
        #    next clamp still advances.
        if window_too_small(
            window_start_ms=window_start,
            window_end_ms=window_end,
            min_window_secs=config.min_window_secs,
        ):
            return await _record_skipped_empty(
                store=store,
                tenant_id=tenant_id,
                window_start=window_start,
                window_end=window_end,
                started_at=now,
                swept=swept,
                reason="window_below_min_secs",
            )

        # 4. Claim the unique window. If the second runner races into
        #    the same window we *find* the prior row and return its
        #    summary — idempotent by design-doc contract.
        try:
            run = await store.open_run(
                tenant_id=tenant_id,
                window_start=window_start,
                window_end=window_end,
                started_at=now,
            )
        except RunWindowConflictError:
            existing = await store.find_run(
                tenant_id=tenant_id,
                window_start=window_start,
                window_end=window_end,
            )
            if existing is None:
                # Should not happen — the unique-index fired so the row
                # exists. Re-raise so the operator sees the corruption.
                raise
            return _summary_from_existing(
                existing,
                bundles_seen=0,
                swept=swept,
            )

        # 5-7. Collect → distill → insert; if anything raises, mark the
        # run failed with the message and re-raise so the caller sees
        # the stack.
        try:
            bundles = collect_bundles(
                paths=sources,
                tenant_id=tenant_id,
                window_start_ms=window_start,
                window_end_ms=window_end,
            )

            if not bundles:
                # Same shape as the too-small short-circuit, but on the
                # collected-rows axis instead of the wall-clock axis.
                await store.finish_run(
                    run.run_id,
                    status=RUN_STATUS_SKIPPED_EMPTY,
                    episodes_written=0,
                    finished_at=_now_ms(),
                )
                return RunSummary(
                    tenant_id=tenant_id,
                    run_id=run.run_id,
                    status=RUN_STATUS_SKIPPED_EMPTY,
                    window_start_ms=window_start,
                    window_end_ms=window_end,
                    episodes_written=0,
                    bundles_seen=0,
                    swept_stale_runs=tuple(swept),
                )

            written, reused = await _distill_and_persist(
                store=store,
                bundles=bundles,
                config=config,
                tenant_id=tenant_id,
                provider=summary_provider,
            )
            await store.finish_run(
                run.run_id,
                status=RUN_STATUS_OK,
                episodes_written=written,
                finished_at=_now_ms(),
            )
            return RunSummary(
                tenant_id=tenant_id,
                run_id=run.run_id,
                status=RUN_STATUS_OK,
                window_start_ms=window_start,
                window_end_ms=window_end,
                episodes_written=written,
                episodes_reused=reused,
                bundles_seen=len(bundles),
                swept_stale_runs=tuple(swept),
            )
        except Exception as exc:
            # Stamp failed so the next pass doesn't get blocked by
            # this run's window — and so an operator can see what
            # broke.
            try:
                await store.finish_run(
                    run.run_id,
                    status=RUN_STATUS_FAILED,
                    episodes_written=0,
                    error_message=str(exc),
                    finished_at=_now_ms(),
                )
            except Exception:  # pragma: no cover  - secondary failure
                logger.exception(
                    "episodes.runner: failed to mark run failed",
                    extra={"run_id": run.run_id},
                )
            raise


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _distill_and_persist(
    *,
    store: EpisodesStore,
    bundles: list[SourceBundle],
    config: EpisodesConfig,
    tenant_id: str,
    provider: SummaryProvider | SummaryFn,
) -> tuple[int, int]:
    """Per-bundle: classify → score → distill → insert.

    Returns ``(written, reused)`` — ``reused`` counts bundles whose
    natural-key probe already had a row from a prior crashed pass.
    """
    written = 0
    reused = 0
    for bundle in bundles:
        kind = classify(bundle)
        importance = score(bundle, kind)

        existing = await store.find_episode_by_natural_key(
            tenant_id=tenant_id,
            started_at=bundle.started_at,
            ended_at=bundle.ended_at,
            kind=kind,
        )
        if existing is not None:
            # Defence-in-depth dedup — the unique-window guard on the
            # run table is the primary; this catches the narrower
            # "same window minted episode A, then crashed; on retry,
            # bundle A reproduces" case.
            reused += 1
            continue

        summary = await distill(
            bundle,
            kind=kind,
            provider=provider,
            provider_alias=config.llm_provider_alias,
            max_messages_per_call=config.max_messages_per_call,
        )

        episode = Episode(
            id=new_episode_id(ts_ms=bundle.ended_at),
            tenant_id=tenant_id,
            started_at=bundle.started_at,
            ended_at=bundle.ended_at,
            kind=kind,
            summary_text=summary.summary_text,
            source_session_keys=_session_keys(bundle),
            source_signal_ids=[s.id for s in bundle.signals],
            source_history_ids=[h.id for h in bundle.history],
            embedding=None,
            embedding_dim=None,
            importance_score=importance,
            distilled_by=summary.distilled_by,
            distilled_at=_now_ms(),
        )
        await store.insert_episode(episode)
        written += 1
    return written, reused


def _session_keys(bundle: SourceBundle) -> list[str]:
    """All distinct session_keys touched by a bundle.

    Includes the bundle key itself plus any keys carried on hooks
    (those are linked at hook-write time and may differ from the
    bundle's session_key in the orphan-bucket case where the bundle
    key is None but a hook still references a session).
    """
    keys: list[str] = []
    seen: set[str] = set()
    if bundle.session_key is not None:
        keys.append(bundle.session_key)
        seen.add(bundle.session_key)
    for hook in bundle.hooks:
        if hook.session_key is not None and hook.session_key not in seen:
            keys.append(hook.session_key)
            seen.add(hook.session_key)
    return keys


async def _record_skipped_empty(
    *,
    store: EpisodesStore,
    tenant_id: str,
    window_start: int,
    window_end: int,
    started_at: int,
    swept: list[str],
    reason: str,
) -> RunSummary:
    """Open + finish a ``skipped_empty`` row in one shot.

    Used for the too-small-window guard; we still want a run row for
    operators to grep, and the unique-window guard prevents
    double-recording the same skip.
    """
    try:
        run = await store.open_run(
            tenant_id=tenant_id,
            window_start=window_start,
            window_end=window_end,
            started_at=started_at,
        )
    except RunWindowConflictError:
        existing = await store.find_run(
            tenant_id=tenant_id,
            window_start=window_start,
            window_end=window_end,
        )
        if existing is None:
            raise
        return _summary_from_existing(
            existing, bundles_seen=0, swept=swept
        )

    await store.finish_run(
        run.run_id,
        status=RUN_STATUS_SKIPPED_EMPTY,
        episodes_written=0,
        error_message=reason,
        finished_at=_now_ms(),
    )
    return RunSummary(
        tenant_id=tenant_id,
        run_id=run.run_id,
        status=RUN_STATUS_SKIPPED_EMPTY,
        window_start_ms=window_start,
        window_end_ms=window_end,
        episodes_written=0,
        bundles_seen=0,
        swept_stale_runs=tuple(swept),
        error_message=reason,
    )


def _summary_from_existing(
    run: DistillationRun,
    *,
    bundles_seen: int,
    swept: list[str],
) -> RunSummary:
    """Lift an existing run row into a :class:`RunSummary`."""
    return RunSummary(
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        status=run.status,
        window_start_ms=run.window_start,
        window_end_ms=run.window_end,
        episodes_written=run.episodes_written,
        bundles_seen=bundles_seen,
        swept_stale_runs=tuple(swept),
        error_message=run.error_message,
    )


def _disabled_skip(*, tenant_id: str, now_ms: int) -> RunSummary:
    """Synthetic summary for ``config.enabled=False``.

    ``run_id`` is empty so callers can short-circuit logging without
    pretending a row exists.
    """
    return RunSummary(
        tenant_id=tenant_id,
        run_id="",
        status=RUN_STATUS_SKIPPED_EMPTY,
        window_start_ms=now_ms,
        window_end_ms=now_ms,
        episodes_written=0,
        bundles_seen=0,
        error_message="episodes_disabled",
    )


def _now_ms() -> int:
    """Pulled out so tests can monkeypatch the clock."""
    return int(time.time() * 1000)


__all__ = [
    "RunSummary",
    "episodes_run_once",
]
