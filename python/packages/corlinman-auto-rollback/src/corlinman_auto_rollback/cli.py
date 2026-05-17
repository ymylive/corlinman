"""``corlinman-auto-rollback`` CLI entry point.

Python port of ``rust/crates/corlinman-auto-rollback-cli/src/main.rs``.

Thin wrapper that loads a corlinman config, opens ``evolution.sqlite``,
builds an :class:`~corlinman_auto_rollback.AutoRollbackMonitor` against
an injected :class:`~corlinman_auto_rollback.Applier`, and runs one
:meth:`AutoRollbackMonitor.run_once` pass. Designed to be invoked as a
subprocess job from a scheduler — same shape as the Rust binary.

In Python the gateway's ``EvolutionApplier`` does not yet have a
sibling in the workspace (see TODOs in ``__init__``). For now the CLI
loads an applier via a ``--applier`` import path
(``module:callable``) which must return an :class:`Applier`; when
omitted the CLI hard-fails with a clear message rather than running
with a stub. Operators wire the gateway-side applier in once the port
of ``corlinman-gateway::evolution_applier`` lands.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from corlinman_evolution_store import EvolutionStore, HistoryRepo, ProposalsRepo

from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.monitor import AutoRollbackMonitor, RunSummary
from corlinman_auto_rollback.revert import Applier

logger = logging.getLogger("corlinman_auto_rollback")


PROG_NAME = "corlinman-auto-rollback"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description=(
            "AutoRollback — watches recently-applied EvolutionProposals "
            "for metrics regression and auto-reverts via an Applier."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run-once",
        help=(
            "Run one auto-rollback pass: list applied proposals in the "
            "grace window, compute metric deltas, revert anything whose "
            "delta breaches threshold."
        ),
    )
    run.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Path to the corlinman config (corlinman.toml). Reads "
            "[evolution.auto_rollback], [evolution.observer].db_path, "
            "[server].data_dir."
        ),
    )
    run.add_argument(
        "--max-proposals",
        type=int,
        default=None,
        help=(
            "Per-run cap on proposals inspected; overrides the monitor's "
            "default (50). Useful for one-off backfills."
        ),
    )
    run.add_argument(
        "--applier",
        type=str,
        default=None,
        help=(
            "Import path 'module:callable' for an Applier factory. The "
            "callable is invoked with the loaded EvolutionStore and "
            "config dict and must return an Applier. Mirrors the way the "
            "Rust binary wires in EvolutionApplier from corlinman-gateway."
        ),
    )
    run.add_argument(
        "--evolution-db",
        type=Path,
        default=None,
        help=(
            "Override [evolution.observer].db_path. Lets the CLI run "
            "against a test DB without rewriting the config."
        ),
    )
    run.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON on stdout.",
    )
    run.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )
    return parser


def _load_config(path: Path) -> dict[str, Any]:
    """Best-effort TOML load — propagates FileNotFoundError /
    tomllib.TOMLDecodeError so the CLI exits with a clean message."""
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _load_auto_rollback_config(
    raw: dict[str, Any],
) -> EvolutionAutoRollbackConfig:
    """Pull ``[evolution.auto_rollback]`` out of the workspace TOML.

    Missing section -> defaults. Mirrors the Rust crate's defaults
    (master switch off, 72h grace, 50% err-rate threshold, etc.).
    """
    evolution = raw.get("evolution", {})
    if not isinstance(evolution, dict):
        return EvolutionAutoRollbackConfig()
    ar = evolution.get("auto_rollback", {})
    if not isinstance(ar, dict):
        return EvolutionAutoRollbackConfig()

    th_raw = ar.get("thresholds", {})
    th = AutoRollbackThresholds()
    if isinstance(th_raw, dict):
        th = AutoRollbackThresholds(
            default_err_rate_delta_pct=float(
                th_raw.get("default_err_rate_delta_pct", th.default_err_rate_delta_pct)
            ),
            default_p95_latency_delta_pct=float(
                th_raw.get(
                    "default_p95_latency_delta_pct", th.default_p95_latency_delta_pct
                )
            ),
            signal_window_secs=int(
                th_raw.get("signal_window_secs", th.signal_window_secs)
            ),
            min_baseline_signals=int(
                th_raw.get("min_baseline_signals", th.min_baseline_signals)
            ),
        )

    return EvolutionAutoRollbackConfig(
        enabled=bool(ar.get("enabled", False)),
        grace_window_hours=int(ar.get("grace_window_hours", 72)),
        thresholds=th,
    )


def _resolve_evolution_db_path(raw: dict[str, Any], override: Path | None) -> Path:
    """Resolve evolution.sqlite path the same way the Rust binary does:
    explicit --evolution-db wins, otherwise ``[evolution.observer].db_path``,
    otherwise fall back to ``<data_dir>/evolution.sqlite`` from
    ``[server].data_dir`` (with ``CORLINMAN_DATA_DIR`` env override)."""
    if override is not None:
        return override

    evolution = raw.get("evolution", {})
    if isinstance(evolution, dict):
        observer = evolution.get("observer", {})
        if isinstance(observer, dict):
            db_path = observer.get("db_path")
            if isinstance(db_path, str) and db_path:
                return Path(db_path)

    env_dir = os.environ.get("CORLINMAN_DATA_DIR")
    if env_dir:
        return Path(env_dir) / "evolution.sqlite"

    server = raw.get("server", {})
    if isinstance(server, dict):
        data_dir = server.get("data_dir")
        if isinstance(data_dir, str) and data_dir:
            return Path(data_dir) / "evolution.sqlite"

    # Last-ditch default mirrors the Rust binary's expectation for the
    # gateway-style deployment.
    return Path("/data/evolution.sqlite")


def _load_applier_factory(spec: str):
    """Resolve ``module:callable`` into the actual callable.

    Mirrors the import-path injection pattern other Python tools use
    (uvicorn, gunicorn). Kept here rather than in the library so the
    monitor module itself stays free of dynamic imports.
    """
    if ":" not in spec:
        raise ValueError(
            f"--applier must be 'module:callable', got {spec!r}"
        )
    module_path, attr = spec.split(":", 1)
    module = importlib.import_module(module_path)
    factory = getattr(module, attr, None)
    if factory is None or not callable(factory):
        raise ValueError(
            f"--applier {spec!r}: {attr} is not callable on {module_path}"
        )
    return factory


def _summary_to_dict(summary: RunSummary) -> dict[str, int]:
    return asdict(summary)


def _print_summary(summary: RunSummary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_summary_to_dict(summary), indent=2))
        return
    print(f"proposals_inspected: {summary.proposals_inspected}")
    print(f"thresholds_breached: {summary.thresholds_breached}")
    print(f"rollbacks_triggered: {summary.rollbacks_triggered}")
    print(f"rollbacks_succeeded: {summary.rollbacks_succeeded}")
    print(f"rollbacks_failed:    {summary.rollbacks_failed}")
    print(f"errors:              {summary.errors}")


async def _run_once_async(
    *,
    config_path: Path,
    evolution_db_override: Path | None,
    applier_spec: str | None,
    max_proposals: int | None,
) -> RunSummary:
    raw = _load_config(config_path)
    ar_cfg = _load_auto_rollback_config(raw)

    if not ar_cfg.enabled:
        logger.error(
            "auto_rollback: [evolution.auto_rollback].enabled = false — "
            "refusing to run. Set it to true once metrics_baseline rows "
            "have populated, or remove the cron job."
        )
        raise SystemExit(2)

    if applier_spec is None:
        logger.error(
            "auto_rollback: --applier is required. Provide an import path "
            "'module:callable' that returns an Applier (e.g. the gateway-"
            "side EvolutionApplier factory)."
        )
        raise SystemExit(2)

    evolution_db = _resolve_evolution_db_path(raw, evolution_db_override)
    logger.info(
        "auto_rollback: opening evolution.sqlite at %s (grace_window=%dh)",
        evolution_db,
        ar_cfg.grace_window_hours,
    )

    store = await EvolutionStore.open(evolution_db)
    try:
        applier_factory = _load_applier_factory(applier_spec)
        applier_obj = applier_factory(store, raw)
        if asyncio.iscoroutine(applier_obj):
            applier_obj = await applier_obj
        if not isinstance(applier_obj, Applier):
            # Protocol is runtime-checkable, so this catches factories
            # that returned something without a ``revert`` method.
            raise TypeError(
                f"--applier factory returned {type(applier_obj).__name__}, "
                f"which does not satisfy the Applier protocol"
            )

        proposals = ProposalsRepo(store.conn)
        history = HistoryRepo(store.conn)
        monitor = AutoRollbackMonitor(
            proposals,
            history,
            store,
            applier_obj,
            ar_cfg,
        )
        if max_proposals is not None:
            monitor = monitor.with_max_proposals_per_run(max_proposals)

        summary = await monitor.run_once()
    finally:
        await store.close()

    logger.info(
        "auto_rollback: run-once complete (inspected=%d breached=%d "
        "triggered=%d succeeded=%d failed=%d errors=%d)",
        summary.proposals_inspected,
        summary.thresholds_breached,
        summary.rollbacks_triggered,
        summary.rollbacks_succeeded,
        summary.rollbacks_failed,
        summary.errors,
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-once":
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            summary = asyncio.run(
                _run_once_async(
                    config_path=args.config,
                    evolution_db_override=args.evolution_db,
                    applier_spec=args.applier,
                    max_proposals=args.max_proposals,
                )
            )
        except SystemExit:
            raise
        except FileNotFoundError as exc:
            logger.error("auto_rollback: %s", exc)
            return 2
        except tomllib.TOMLDecodeError as exc:
            logger.error("auto_rollback: failed to parse config TOML: %s", exc)
            return 2
        _print_summary(summary, as_json=args.json)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error exits, but appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
