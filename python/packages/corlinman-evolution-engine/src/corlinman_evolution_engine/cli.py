"""``corlinman-evolution-engine`` CLI entry point.

Phase 2 shipped ``run-once``; Phase 3 W3-A adds ``consolidate-once``
which drives the chunk decay → consolidation pipeline (decay-score
threshold sweep → file ``memory_op`` proposals targeting
``consolidate_chunk:<id>`` → operator review path).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from corlinman_evolution_engine.consolidation import (
    ConsolidationConfig,
    ConsolidationSummary,
    consolidation_run_once,
)
from corlinman_evolution_engine.engine import (
    BudgetConfig,
    EngineConfig,
    EvolutionEngine,
    RunSummary,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corlinman-evolution-engine",
        description=(
            "Phase 2 EvolutionEngine: read evolution_signals, scan kb.sqlite "
            "for near-duplicate chunks, file memory_op proposals."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-once", help="Run a single engine pass.")
    run.add_argument(
        "--evolution-db",
        type=Path,
        default=Path("/data/evolution.sqlite"),
        help="Path to evolution.sqlite (default: %(default)s).",
    )
    run.add_argument(
        "--kb-db",
        type=Path,
        default=Path("/data/kb.sqlite"),
        help="Path to kb.sqlite (default: %(default)s).",
    )
    run.add_argument("--lookback-days", type=int, default=7)
    run.add_argument("--min-cluster-size", type=int, default=3)
    run.add_argument("--max-proposals-per-run", type=int, default=10)
    run.add_argument("--run-budget-seconds", type=int, default=60)
    run.add_argument("--similarity-threshold", type=float, default=0.95)
    run.add_argument("--max-chunks-scanned", type=int, default=5_000)
    run.add_argument(
        "--budget-config",
        type=Path,
        default=None,
        help=(
            "Path to the workspace TOML config containing "
            "[evolution.budget]. When omitted, budget enforcement is "
            "disabled (the Phase 2 / 3 W1-A behavior)."
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

    cons = sub.add_parser(
        "consolidate-once",
        help=(
            "Phase 3 W3-A: scan kb.sqlite for chunks above the "
            "promotion threshold and file memory_op proposals "
            "targeting consolidate_chunk:<id>."
        ),
    )
    cons.add_argument(
        "--evolution-db",
        type=Path,
        default=Path("/data/evolution.sqlite"),
        help="Path to evolution.sqlite (default: %(default)s).",
    )
    cons.add_argument(
        "--kb-db",
        type=Path,
        default=Path("/data/kb.sqlite"),
        help="Path to kb.sqlite (default: %(default)s).",
    )
    cons.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the workspace TOML config containing "
            "[memory.consolidation]. When omitted, the built-in "
            "defaults are used (enabled=true, threshold=0.65, "
            "max_promotions_per_run=50)."
        ),
    )
    cons.add_argument(
        "--json",
        action="store_true",
        help="Emit the run summary as JSON on stdout.",
    )
    cons.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )
    return parser


def _load_budget_config(path: Path | None) -> BudgetConfig:
    """Parse ``[evolution.budget]`` out of the workspace TOML.

    Missing file or missing section → default ``BudgetConfig`` (disabled).
    Same passthrough path the Rust binaries take, just narrowed to one
    section so the engine doesn't need to model the whole workspace config.
    """
    if path is None:
        return BudgetConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return BudgetConfig()
    section = data.get("evolution", {}).get("budget", {})
    if not isinstance(section, dict):
        return BudgetConfig()
    per_kind_raw = section.get("per_kind", {})
    per_kind: dict[str, int] = {}
    if isinstance(per_kind_raw, dict):
        for k, v in per_kind_raw.items():
            if isinstance(k, str) and isinstance(v, int):
                per_kind[k] = v
    return BudgetConfig(
        enabled=bool(section.get("enabled", False)),
        weekly_total=int(section.get("weekly_total", 15)),
        per_kind=per_kind,
    )


def _config_from_args(args: argparse.Namespace) -> EngineConfig:
    return EngineConfig(
        db_path=args.evolution_db,
        kb_path=args.kb_db,
        lookback_days=args.lookback_days,
        min_cluster_size=args.min_cluster_size,
        max_proposals_per_run=args.max_proposals_per_run,
        run_budget_seconds=args.run_budget_seconds,
        similarity_threshold=args.similarity_threshold,
        max_chunks_scanned=args.max_chunks_scanned,
        budget=_load_budget_config(args.budget_config),
    )


def _load_consolidation_config(path: Path | None) -> ConsolidationConfig:
    """Parse ``[memory.consolidation]`` out of the workspace TOML.

    Missing file or missing section → default ``ConsolidationConfig``.
    Same passthrough shape the engine uses for ``--budget-config`` so
    operators only juggle one TOML.
    """
    if path is None:
        return ConsolidationConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return ConsolidationConfig()
    section = data.get("memory", {}).get("consolidation", {})
    if not isinstance(section, dict):
        return ConsolidationConfig()
    return ConsolidationConfig(
        enabled=bool(section.get("enabled", True)),
        promotion_threshold=float(section.get("promotion_threshold", 0.65)),
        max_promotions_per_run=int(section.get("max_promotions_per_run", 50)),
    )


def _print_consolidation_summary(
    summary: ConsolidationSummary,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(json.dumps(asdict(summary), indent=2, default=str))
        return
    if summary.skipped_disabled:
        print("consolidation: master switch disabled; nothing to do")
        return
    print(f"candidates_found:   {summary.candidates_found}")
    print(f"proposals_written:  {summary.proposals_written}")
    print(f"skipped_existing:   {summary.skipped_existing}")
    print(f"elapsed_seconds:    {summary.elapsed_seconds:.2f}")


def _print_summary(summary: RunSummary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(asdict(summary), indent=2, default=str))
        return
    print(f"signals_loaded:        {summary.signals_loaded}")
    print(f"clusters_found:        {summary.clusters_found}")
    print(f"duplicate_pairs_found: {summary.duplicate_pairs_found}")
    print(f"proposals_written:     {summary.proposals_written}")
    print(f"skipped_existing:      {summary.skipped_existing}")
    print(f"truncated_by_cap:      {summary.truncated_by_cap}")
    print(f"skipped_by_budget:     {summary.skipped_by_budget}")
    print(f"proposals_skipped_budget: {summary.proposals_skipped_budget}")
    print(f"elapsed_seconds:       {summary.elapsed_seconds:.2f}")
    if summary.cluster_summaries:
        print("clusters:")
        for line in summary.cluster_summaries:
            print(f"  - {line}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-once":
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        engine = EvolutionEngine(_config_from_args(args))
        summary = asyncio.run(engine.run_once())
        _print_summary(summary, as_json=args.json)
        return 0

    if args.command == "consolidate-once":
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        cfg = _load_consolidation_config(args.config)
        summary = asyncio.run(
            consolidation_run_once(
                config=cfg,
                kb_db_path=args.kb_db,
                evolution_db_path=args.evolution_db,
            )
        )
        _print_consolidation_summary(summary, as_json=args.json)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error exits, but appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
