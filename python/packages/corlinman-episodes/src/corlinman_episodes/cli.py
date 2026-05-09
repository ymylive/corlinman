"""``corlinman-episodes`` CLI — scheduled / one-shot distillation entry.

This is the iter-6 surface: a thin wrapper around
:func:`corlinman_episodes.runner.episodes_run_once` and
:func:`corlinman_episodes.embed.populate_pending_embeddings` so the
production scheduler can fire the runner via ``Subprocess`` job (see
``rust/crates/corlinman-scheduler/src/jobs.rs``) and operators can
trigger one-off catch-up passes from the shell.

Subcommands:

- ``distill-once`` — one pass over a window. Default window comes from
  ``EpisodesConfig.distillation_window_hours``; ``--window-hours N``
  overrides for catch-up runs (e.g. *"resync the last 168h after I
  swapped the LLM provider"*).
- ``embed-pending`` — second-pass embedding sweep. Idempotent — rows
  whose embedding was already populated are skipped by the SQL filter
  in :meth:`EpisodesStore.fetch_pending_embeddings`.

The command's contract with the scheduler is:

* Exit 0 on success (``ok``, ``skipped_empty``, idempotent re-run that
  reused the prior run row).
* Exit 1 on a runner-level failure (provider raised, store IO error).
  The scheduler folds the non-zero exit into ``EngineRunFailed`` on
  the hook bus, same as the evolution-engine subprocess.
* ``--json`` emits a single JSON line on stdout — newest-first
  caller-friendly format for ``corlinman-cli doctor`` consumers.

Provider wiring (LLM + embedding) is intentionally pluggable:

* Tests inject :class:`SummaryProvider` / :class:`EmbeddingProvider`
  callables directly via :func:`run_distill_once` (the lib-level
  function the CLI delegates to).
* The shipped binary ships a built-in *stub-only* path —
  ``--stub-summary "..."`` — that emits a constant string for every
  bundle. Production wiring of the real ``corlinman-providers`` /
  ``corlinman-embedding`` adapters is out of scope for this iter
  (callers wire it explicitly via ``run_distill_once`` / the admin
  route in iter 9). The stub keeps the scheduler subprocess
  exercise-able end-to-end without a real LLM.

A registered ``provider_factory`` hook lets a third party (the
gateway boot, an integration test) swap in a real provider without
touching this module — see :func:`register_summary_provider_factory`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from pathlib import Path

from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.distiller import (
    SummaryFn,
    SummaryProvider,
    make_constant_provider,
)
from corlinman_episodes.embed import (
    EmbeddingFn,
    EmbeddingProvider,
    EmbedSummary,
    populate_pending_embeddings,
)
from corlinman_episodes.runner import RunSummary, episodes_run_once
from corlinman_episodes.sources import SourcePaths
from corlinman_episodes.store import EpisodesStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider factory registry
# ---------------------------------------------------------------------------
#
# Production deployments (the gateway boot, the ``corlinman-scheduler``
# subprocess invocation, an integration harness) register a real
# provider factory before invoking ``main``. The default is None — the
# CLI then requires ``--stub-summary`` so tests + scheduler smoke runs
# work without a network round-trip.

#: Factory: (config, alias) → SummaryProvider/SummaryFn. Registered by
#: the gateway boot when it wires the real ``corlinman-providers``
#: adapter; left unset in tests and the stub-only path.
_summary_factory: Callable[[EpisodesConfig, str], SummaryProvider | SummaryFn] | None = (
    None
)
_embedding_factory: (
    Callable[
        [EpisodesConfig, str], tuple[EmbeddingProvider | EmbeddingFn, int]
    ]
    | None
) = None


def register_summary_provider_factory(
    factory: Callable[[EpisodesConfig, str], SummaryProvider | SummaryFn] | None,
) -> None:
    """Plug a real LLM provider into the CLI without import cycles.

    The factory takes the loaded :class:`EpisodesConfig` and the
    operator-set provider alias (``config.llm_provider_alias``) and
    returns either a :class:`SummaryProvider` or a bare async callable.
    Passing ``None`` clears the registration — useful in test teardown.

    Tests don't register a factory; they call :func:`run_distill_once`
    directly with an inline stub. Production wiring (gateway boot, the
    iter 9 admin route) registers the factory so the same ``main()``
    entry works for the scheduler subprocess shape.
    """
    global _summary_factory
    _summary_factory = factory


def register_embedding_provider_factory(
    factory: Callable[
        [EpisodesConfig, str], tuple[EmbeddingProvider | EmbeddingFn, int]
    ]
    | None,
) -> None:
    """Plug an embedding-router factory into the CLI.

    Returns ``(provider, dim)`` so the CLI can quote the dimension
    explicitly to :func:`populate_pending_embeddings` (per design OQ 4
    a dim mismatch is a hard-error at write time).
    """
    global _embedding_factory
    _embedding_factory = factory


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _load_episodes_config(path: Path | None) -> EpisodesConfig:
    """Parse ``[episodes]`` out of a workspace TOML.

    Missing file or missing section → default :class:`EpisodesConfig`.
    Mirrors :func:`corlinman_evolution_engine.cli._load_consolidation_config`'s
    shape so operators juggle one TOML for both subsystems.
    """
    if path is None:
        return EpisodesConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return EpisodesConfig()
    section = data.get("episodes", {})
    if not isinstance(section, dict):
        return EpisodesConfig()
    # Pull only known fields. Unknown keys are ignored rather than
    # raising — the design ships defaults for every knob and an
    # operator's stale ``episodes.toml`` shouldn't break a fresh boot.
    fields = {
        "enabled": bool,
        "schedule": str,
        "distillation_window_hours": float,
        "min_session_count_per_episode": int,
        "min_window_secs": int,
        "max_messages_per_call": int,
        "llm_provider_alias": str,
        "embedding_provider_alias": str,
        "max_episodes_per_query": int,
        "last_week_top_n": int,
        "cold_archive_days": int,
        "run_stale_after_secs": int,
    }
    kwargs: dict[str, object] = {}
    for name, conv in fields.items():
        if name in section:
            try:
                kwargs[name] = conv(section[name])
            except (TypeError, ValueError):
                # Bad type → fall back to default for this field.
                continue
    return EpisodesConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Library-level distill-once
# ---------------------------------------------------------------------------


async def run_distill_once(
    *,
    config: EpisodesConfig,
    episodes_db: Path,
    sources: SourcePaths,
    summary_provider: SummaryProvider | SummaryFn,
    tenant_id: str = DEFAULT_TENANT_ID,
    window_hours_override: float | None = None,
    now_ms: int | None = None,
) -> RunSummary:
    """Library-level distill-once — preferred call site for tests.

    Tests construct the config + provider inline and call this. The
    CLI ``main()`` is a thin shell that builds these arguments out of
    ``argv`` and the registered factory.

    ``window_hours_override`` rebuilds the config with a different
    ``distillation_window_hours`` so a one-off catch-up pass
    (``--window-hours 168``) doesn't have to mutate the operator's
    TOML. The frozen dataclass means we use :func:`dataclasses.replace`
    to lift the override.
    """
    effective = (
        replace(config, distillation_window_hours=float(window_hours_override))
        if window_hours_override is not None
        else config
    )
    return await episodes_run_once(
        config=effective,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=summary_provider,
        tenant_id=tenant_id,
        now_ms=now_ms,
    )


async def run_embed_pending(
    *,
    config: EpisodesConfig,
    episodes_db: Path,
    embedding_provider: EmbeddingProvider | EmbeddingFn,
    embedding_dim: int,
    tenant_id: str = DEFAULT_TENANT_ID,
    batch_size: int = 32,
    max_episodes: int | None = None,
) -> EmbedSummary:
    """Library-level embed-pending — opens the store + delegates."""
    async with EpisodesStore(episodes_db) as store:
        return await populate_pending_embeddings(
            config=config,
            store=store,
            provider=embedding_provider,
            embedding_dim=embedding_dim,
            tenant_id=tenant_id,
            batch_size=batch_size,
            max_episodes=max_episodes,
        )


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _add_common_db_args(parser: argparse.ArgumentParser) -> None:
    """Per-tenant DB paths shared between subcommands.

    Defaults match the design's ``<data_dir>/tenants/<slug>/...`` layout
    only loosely — the CLI is the *plumbing*; the gateway boot supplies
    explicit paths for the active tenant. The defaults are placeholders
    that fail loudly rather than read someone else's data.
    """
    parser.add_argument(
        "--episodes-db",
        type=Path,
        required=True,
        help="Per-tenant episodes.sqlite path.",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT_ID,
        help="Tenant id (default: %(default)s).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Workspace TOML containing [episodes]. Missing file → "
            "built-in defaults."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as a JSON line on stdout.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corlinman-episodes",
        description=(
            "Phase 4 W4 D1 episodic memory: distil sessions / signals / "
            "history into narrative-shaped, importance-ranked, "
            "tenant-scoped episodes."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    distill = sub.add_parser(
        "distill-once",
        help="Run a single distillation pass on the configured window.",
    )
    _add_common_db_args(distill)
    distill.add_argument(
        "--sessions-db",
        type=Path,
        required=True,
        help="Path to per-tenant sessions.sqlite (read-only join source).",
    )
    distill.add_argument(
        "--evolution-db",
        type=Path,
        required=True,
        help="Path to per-tenant evolution.sqlite (signals + history).",
    )
    distill.add_argument(
        "--hook-events-db",
        type=Path,
        required=True,
        help="Path to hook_events.sqlite.",
    )
    distill.add_argument(
        "--identity-db",
        type=Path,
        default=None,
        help="Optional verification_phrases store; omit if absent.",
    )
    distill.add_argument(
        "--window-hours",
        type=float,
        default=None,
        help=(
            "Override the rolling window length (hours). Defaults to "
            "EpisodesConfig.distillation_window_hours; pass a larger "
            "value for catch-up after an outage."
        ),
    )
    distill.add_argument(
        "--stub-summary",
        type=str,
        default=None,
        help=(
            "Use a constant-string summary provider instead of the "
            "registered LLM factory. Required when no factory is "
            "registered (smoke tests, scheduler dry runs)."
        ),
    )
    distill.add_argument(
        "--now-ms",
        type=int,
        default=None,
        help=argparse.SUPPRESS,  # forensic / test-only
    )

    embed = sub.add_parser(
        "embed-pending",
        help="Backfill embedding BLOBs for episodes written with NULL vector.",
    )
    _add_common_db_args(embed)
    embed.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Embedding batch size (default: %(default)s).",
    )
    embed.add_argument(
        "--max-episodes",
        type=int,
        default=None,
        help="Cap rows touched per pass (default: unbounded).",
    )
    embed.add_argument(
        "--stub-embedding-dim",
        type=int,
        default=None,
        help=(
            "Stub mode: emit deterministic zero vectors of this "
            "dimension. Required when no embedding factory is "
            "registered."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def _resolve_summary_provider(
    config: EpisodesConfig,
    *,
    stub_summary: str | None,
) -> SummaryProvider | SummaryFn:
    """Pick the summary provider for one CLI invocation.

    Precedence:

    1. ``--stub-summary "..."`` — always honoured. Used by the
       scheduler smoke job and tests that go through ``main()``.
    2. ``register_summary_provider_factory`` factory, if registered.
    3. Hard error — refuse to silently no-op.
    """
    if stub_summary is not None:
        return make_constant_provider(stub_summary)
    if _summary_factory is not None:
        return _summary_factory(config, config.llm_provider_alias)
    raise SystemExit(
        "corlinman-episodes: no summary provider registered. Pass "
        "--stub-summary or register a factory via "
        "register_summary_provider_factory."
    )


def _resolve_embedding_provider(
    config: EpisodesConfig,
    *,
    stub_dim: int | None,
) -> tuple[EmbeddingProvider | EmbeddingFn, int]:
    """Pick the embedding provider + dim for one CLI invocation.

    Same precedence shape as :func:`_resolve_summary_provider`. The
    stub returns a deterministic zero-vector of the requested
    dimension — useful for the scheduler smoke run + the iter-7
    integration tests that just want a non-NULL BLOB.
    """
    if stub_dim is not None:

        async def _zero_provider(texts: Sequence[str]) -> list[list[float]]:
            return [[0.0] * stub_dim for _ in texts]

        return _zero_provider, int(stub_dim)
    if _embedding_factory is not None:
        return _embedding_factory(config, config.embedding_provider_alias)
    raise SystemExit(
        "corlinman-episodes: no embedding provider registered. Pass "
        "--stub-embedding-dim or register a factory via "
        "register_embedding_provider_factory."
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_run_summary(summary: RunSummary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(asdict(summary), default=str))
        return
    print(f"tenant_id:         {summary.tenant_id}")
    print(f"run_id:            {summary.run_id}")
    print(f"status:            {summary.status}")
    print(f"window:            [{summary.window_start_ms}, {summary.window_end_ms})")
    print(f"episodes_written:  {summary.episodes_written}")
    print(f"episodes_reused:   {summary.episodes_reused}")
    print(f"bundles_seen:      {summary.bundles_seen}")
    if summary.swept_stale_runs:
        print(f"swept_stale_runs:  {','.join(summary.swept_stale_runs)}")
    if summary.error_message:
        print(f"error_message:     {summary.error_message}")


def _print_embed_summary(summary: EmbedSummary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(asdict(summary), default=str))
        return
    print(f"tenant_id:      {summary.tenant_id}")
    print(f"embedded:       {summary.embedded}")
    print(f"failed:         {summary.failed}")
    print(f"bytes_written:  {summary.bytes_written}")
    if summary.failed_episode_ids:
        print(f"failed_ids:     {','.join(summary.failed_episode_ids)}")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """argparse → asyncio.run → exit code.

    Returns 0 on terminal success (``ok``, ``skipped_empty``,
    idempotent reuse) and 1 on a runner-level exception. The
    scheduler maps the exit code to ``EngineRunCompleted`` /
    ``EngineRunFailed`` on the hook bus.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = _load_episodes_config(args.config)

    if args.command == "distill-once":
        sources = SourcePaths(
            sessions_db=args.sessions_db,
            evolution_db=args.evolution_db,
            hook_events_db=args.hook_events_db,
            identity_db=args.identity_db,
        )
        provider = _resolve_summary_provider(
            config, stub_summary=args.stub_summary
        )
        try:
            summary = asyncio.run(
                run_distill_once(
                    config=config,
                    episodes_db=args.episodes_db,
                    sources=sources,
                    summary_provider=provider,
                    tenant_id=args.tenant,
                    window_hours_override=args.window_hours,
                    now_ms=args.now_ms,
                )
            )
        except Exception as exc:
            logger.error("episodes: distill-once failed", exc_info=exc)
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_run_summary(summary, as_json=args.json)
        return 0

    if args.command == "embed-pending":
        provider, dim = _resolve_embedding_provider(
            config, stub_dim=args.stub_embedding_dim
        )
        try:
            summary = asyncio.run(
                run_embed_pending(
                    config=config,
                    episodes_db=args.episodes_db,
                    embedding_provider=provider,
                    embedding_dim=dim,
                    tenant_id=args.tenant,
                    batch_size=args.batch_size,
                    max_episodes=args.max_episodes,
                )
            )
        except Exception as exc:
            logger.error("episodes: embed-pending failed", exc_info=exc)
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_embed_summary(summary, as_json=args.json)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error raises; appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "main",
    "register_embedding_provider_factory",
    "register_summary_provider_factory",
    "run_distill_once",
    "run_embed_pending",
]
