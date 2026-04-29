"""``corlinman-persona`` CLI entry point.

Three subcommands:

- ``decay-once`` — sweep every row, apply :func:`apply_decay` against the
  elapsed wall-time since each row's ``updated_at_ms``, write back. The
  scheduler runs this hourly via the example config; the time math
  works just as well for ad-hoc operator runs.
- ``show --agent-id <id>`` — print the current row as JSON.
- ``reset --agent-id <id>`` — delete the row. The next seeder pass
  re-creates it from the YAML defaults; this is the operator escape
  hatch and is intentionally not bound to the EvolutionLoop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from corlinman_persona.decay import DecayConfig, apply_decay
from corlinman_persona.state import PersonaState
from corlinman_persona.store import DEFAULT_TENANT_ID, PersonaStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corlinman-persona",
        description=(
            "Persona persistence helper — manages agent_state.sqlite "
            "(mood / fatigue / recent_topics) across sessions."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/data/agent_state.sqlite"),
        help="Path to agent_state.sqlite (default: %(default)s).",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=DEFAULT_TENANT_ID,
        help=(
            "Tenant scope for read/write (default: %(default)s). Phase 4 "
            "multi-tenant deployments override per-call."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO logging.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    decay = sub.add_parser(
        "decay-once",
        help="Apply mood/fatigue/topics decay to every row in the store.",
    )
    decay.add_argument(
        "--fatigue-recovery-per-hour",
        type=float,
        default=DecayConfig().fatigue_recovery_per_hour,
    )
    decay.add_argument(
        "--mood-decay-per-hour",
        type=float,
        default=DecayConfig().mood_decay_per_hour,
    )
    decay.add_argument(
        "--recent-topics-decay-per-day",
        type=int,
        default=DecayConfig().recent_topics_decay_per_day,
    )
    decay.add_argument(
        "--tired-to-neutral-below",
        type=float,
        default=DecayConfig().tired_to_neutral_below,
    )

    show = sub.add_parser("show", help="Print one agent's persona row as JSON.")
    show.add_argument("--agent-id", type=str, required=True)

    reset = sub.add_parser(
        "reset",
        help="Delete an agent's persona row (next seeder pass re-creates it).",
    )
    reset.add_argument("--agent-id", type=str, required=True)

    return parser


def _config_from_args(args: argparse.Namespace) -> DecayConfig:
    return DecayConfig(
        fatigue_recovery_per_hour=args.fatigue_recovery_per_hour,
        mood_decay_per_hour=args.mood_decay_per_hour,
        recent_topics_decay_per_day=args.recent_topics_decay_per_day,
        tired_to_neutral_below=args.tired_to_neutral_below,
    )


async def _run_decay(db_path: Path, config: DecayConfig, tenant_id: str) -> int:
    """Sweep all rows in ``tenant_id``, apply decay, count how many changed.

    Each row's ``updated_at_ms`` is the reference clock — we compute
    ``hours_elapsed`` per row so a sleeping store doesn't apply a
    blanket "since the last cron tick" delta. Rows whose decay produced
    no change (e.g. fatigue already at 0, no topics, mood unchanged)
    are still upserted with refreshed timestamps so the next decay
    tick has a tighter delta to work with.
    """
    now_ms = int(time.time() * 1000)
    changed = 0
    async with PersonaStore(db_path) as store:
        rows = await store.list_all(tenant_id=tenant_id)
        for row in rows:
            hours = max(0.0, (now_ms - row.updated_at_ms) / 3_600_000.0)
            new_state = apply_decay(row, hours, config)
            new_state = PersonaState(
                agent_id=new_state.agent_id,
                mood=new_state.mood,
                fatigue=new_state.fatigue,
                recent_topics=new_state.recent_topics,
                # Stamp "now" so we don't double-count the elapsed hours
                # on the next sweep.
                updated_at_ms=now_ms,
                state_json=new_state.state_json,
            )
            await store.upsert(new_state, tenant_id=tenant_id)
            if (
                new_state.mood != row.mood
                or new_state.fatigue != row.fatigue
                or new_state.recent_topics != row.recent_topics
            ):
                changed += 1
    return changed


async def _run_show(db_path: Path, agent_id: str, tenant_id: str) -> int:
    async with PersonaStore(db_path) as store:
        state = await store.get(agent_id, tenant_id=tenant_id)
        if state is None:
            print(f"no persona row for agent_id={agent_id!r}", file=sys.stderr)
            return 1
        print(json.dumps(asdict(state), indent=2, default=str))
        return 0


async def _run_reset(db_path: Path, agent_id: str, tenant_id: str) -> int:
    async with PersonaStore(db_path) as store:
        deleted = await store.delete(agent_id, tenant_id=tenant_id)
        if not deleted:
            print(f"no persona row for agent_id={agent_id!r}", file=sys.stderr)
            return 1
        print(f"deleted persona row for agent_id={agent_id!r}")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "decay-once":
        config = _config_from_args(args)
        changed = asyncio.run(_run_decay(args.db, config, args.tenant_id))
        print(f"decay-once: {changed} row(s) changed")
        return 0
    if args.command == "show":
        return asyncio.run(_run_show(args.db, args.agent_id, args.tenant_id))
    if args.command == "reset":
        return asyncio.run(_run_reset(args.db, args.agent_id, args.tenant_id))

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error exits, but appease type-checkers.


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
