"""CLI for the Agent Brain memory curator."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from corlinman_agent_brain.config import CuratorConfig
from corlinman_agent_brain.index_sync import (
    HttpxTransport,
    IndexSyncClient,
    IndexSyncConfig,
)
from corlinman_agent_brain.link_planner import RetrievalProvider
from corlinman_agent_brain.runner import (
    NullRetrievalProvider,
    curate_session,
    memoryhost_retrieval,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corlinman-agent-brain")
    sub = parser.add_subparsers(dest="command", required=True)

    curate = sub.add_parser("curate-session", help="Curate one session into agent memory.")
    curate.add_argument("--session-id", required=True)
    curate.add_argument("--sessions-db", type=Path, required=True)
    curate.add_argument("--vault-root", type=Path, required=True)
    curate.add_argument("--memory-base-url", default="")
    curate.add_argument("--memory-token", default="")
    curate.add_argument("--dry-run", action="store_true")
    curate.add_argument(
        "--allow-empty-provider",
        action="store_true",
        help="Run with a provider that extracts no candidates. Intended for smoke tests.",
    )

    latest = sub.add_parser(
        "curate-latest",
        help="Placeholder command reserved for ranged session curation.",
    )
    latest.add_argument("--agent-id", required=True)

    rebuild = sub.add_parser(
        "rebuild-index",
        help="Placeholder command reserved for vault-to-index rebuild.",
    )
    rebuild.add_argument("--agent-id", required=True)

    for name in ("review", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--draft-id", required=False)
        p.add_argument("--run-id", required=False)

    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "curate-session":
        if not args.allow_empty_provider:
            parser.error(
                "curate-session needs a production extraction provider wiring; "
                "use --allow-empty-provider for smoke tests"
            )

        sync_client = None
        retrieval: RetrievalProvider = NullRetrievalProvider()
        if args.memory_base_url:
            sync_config = IndexSyncConfig(
                base_url=args.memory_base_url,
                bearer_token=args.memory_token or "",
            )
            sync_client = IndexSyncClient(
                HttpxTransport(timeout_ms=sync_config.timeout_ms),
                sync_config,
            )
            # IndexSyncClient structurally implements RetrievalProvider; route
            # through the named adapter so mypy doesn't complain about
            # `retrieval` being narrowed to NullRetrievalProvider at line 69.
            retrieval = memoryhost_retrieval(sync_client)

        report = await curate_session(
            session_id=args.session_id,
            sessions_db=args.sessions_db,
            vault_root=args.vault_root,
            config=CuratorConfig(),
            extraction_provider=_empty_provider,
            retrieval_provider=retrieval,
            sync_client=sync_client,
            dry_run=args.dry_run,
        )
        print(
            json.dumps(
                {
                    "run_id": report.run.run_id,
                    "status": report.run.status,
                    "candidates_total": report.candidates_total,
                    "nodes_written": report.nodes_written,
                    "nodes_synced": report.nodes_synced,
                    "errors": report.run.errors,
                },
                ensure_ascii=False,
            )
        )
        return 0 if report.run.status in {"ok", "skipped_empty"} else 1

    parser.error(f"{args.command} is not implemented yet")
    return 2


async def _empty_provider(*, prompt: str) -> str:
    return "[]"


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = ["async_main", "build_parser", "main"]
