"""``corlinman-evolution-engine`` CLI entry point.

Phase 2 ships exactly one subcommand: ``run-once``. Scheduler integration
(``corlinman-scheduler`` calling this on a cron) is intentionally deferred.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from corlinman_evolution_engine.engine import EngineConfig, EvolutionEngine, RunSummary


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
    )


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

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error exits, but appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
