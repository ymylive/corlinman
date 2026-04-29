"""``corlinman-user-model`` CLI entry point.

Four subcommands:

  * ``distill-once --session-id <id>``      — distil one session.
  * ``distill-recent --since-hours N``      — distil every session that
                                              has had activity in the
                                              last N hours.
  * ``list --user-id <id>``                 — dump traits to stdout.
  * ``prune --confidence-floor F``          — drop traits below ``F``.

argparse on purpose — Click would add a runtime dep we don't otherwise
need.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from corlinman_user_model.distiller import (
    DistillerConfig,
    LLMCaller,
    default_llm_caller,
    distill_session,
)
from corlinman_user_model.store import DEFAULT_TENANT_ID, UserModelStore
from corlinman_user_model.traits import TraitKind


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corlinman-user-model",
        description=(
            "Per-user trait distillation + placeholder data access for the "
            "{{user.*}} family of placeholders."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared DB-path flags — copy/pasted onto each subcommand so the
    # operator can override per-invocation without an env var dance.
    def _add_db_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db-path",
            type=Path,
            default=Path("/data/user_model.sqlite"),
            help="Path to user_model.sqlite (default: %(default)s).",
        )
        p.add_argument(
            "--sessions-db-path",
            type=Path,
            default=Path("/data/sessions.sqlite"),
            help="Path to the gateway sessions.sqlite (default: %(default)s).",
        )

    # Phase 3.1: tenant scoping. Defaults to ``'default'`` until Phase 4
    # wires real tenant ids — operators flipping their gateway over to
    # multi-tenant set this once per CLI invocation.
    def _add_tenant_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--tenant-id",
            type=str,
            default=DEFAULT_TENANT_ID,
            help=(
                "Tenant scope for read/write (default: %(default)s). Phase 4 "
                "multi-tenant deployments override per-call."
            ),
        )

    distill_one = sub.add_parser(
        "distill-once",
        help="Run distillation for a single session_id.",
    )
    _add_db_flags(distill_one)
    _add_tenant_flag(distill_one)
    distill_one.add_argument("--session-id", required=True)
    distill_one.add_argument("--llm-model", default="deepseek-chat")
    distill_one.add_argument("--trait-confidence-floor", type=float, default=0.4)
    distill_one.add_argument(
        "--distill-after-session-turns", type=int, default=5
    )
    distill_one.add_argument(
        "--no-redaction",
        action="store_true",
        help=(
            "Disable the regex PII pass. NEVER use in production — only for "
            "debugging when traits are suspiciously empty on synthetic input."
        ),
    )

    distill_recent = sub.add_parser(
        "distill-recent",
        help="Distil every session with activity in the last N hours.",
    )
    _add_db_flags(distill_recent)
    _add_tenant_flag(distill_recent)
    distill_recent.add_argument("--since-hours", type=float, default=24.0)
    distill_recent.add_argument("--llm-model", default="deepseek-chat")
    distill_recent.add_argument("--trait-confidence-floor", type=float, default=0.4)
    distill_recent.add_argument(
        "--distill-after-session-turns", type=int, default=5
    )
    distill_recent.add_argument("--no-redaction", action="store_true")

    list_cmd = sub.add_parser("list", help="List traits for a user.")
    list_cmd.add_argument("--db-path", type=Path, default=Path("/data/user_model.sqlite"))
    _add_tenant_flag(list_cmd)
    list_cmd.add_argument("--user-id", required=True)
    list_cmd.add_argument(
        "--kind",
        choices=[k.value for k in TraitKind],
        default=None,
        help="Restrict to a single trait kind.",
    )
    list_cmd.add_argument("--min-confidence", type=float, default=0.0)
    list_cmd.add_argument("--json", action="store_true")

    prune = sub.add_parser(
        "prune",
        help="Delete traits below a confidence floor.",
    )
    prune.add_argument("--db-path", type=Path, default=Path("/data/user_model.sqlite"))
    prune.add_argument("--confidence-floor", type=float, default=0.3)

    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


async def _cmd_distill_once(args: argparse.Namespace, llm_caller: LLMCaller) -> int:
    config = DistillerConfig(
        db_path=args.db_path,
        sessions_db_path=args.sessions_db_path,
        distill_after_session_turns=args.distill_after_session_turns,
        trait_confidence_floor=args.trait_confidence_floor,
        redaction_enabled=not args.no_redaction,
        llm_model=args.llm_model,
    )
    traits = await distill_session(
        config,
        args.session_id,
        llm_caller=llm_caller,
        tenant_id=args.tenant_id,
    )
    print(f"distilled {len(traits)} traits for session {args.session_id}")
    for t in traits:
        print(f"  {t.trait_kind.value:10s} {t.confidence:0.2f}  {t.trait_value}")
    return 0


async def _cmd_distill_recent(
    args: argparse.Namespace, llm_caller: LLMCaller
) -> int:
    """Walk every distinct ``session_key`` with a turn newer than the cutoff."""
    cutoff_ms = int((time.time() - args.since_hours * 3600.0) * 1_000)
    session_ids = _list_recent_session_ids(args.sessions_db_path, cutoff_ms)
    if not session_ids:
        print("no recent sessions")
        return 0

    config = DistillerConfig(
        db_path=args.db_path,
        sessions_db_path=args.sessions_db_path,
        distill_after_session_turns=args.distill_after_session_turns,
        trait_confidence_floor=args.trait_confidence_floor,
        redaction_enabled=not args.no_redaction,
        llm_model=args.llm_model,
    )
    total = 0
    for sid in session_ids:
        try:
            traits = await distill_session(
                config, sid, llm_caller=llm_caller, tenant_id=args.tenant_id
            )
        except Exception as exc:
            # Best-effort batch loop: one bad session shouldn't stop the rest.
            print(f"  {sid}: error {exc}", file=sys.stderr)
            continue
        total += len(traits)
        print(f"  {sid}: {len(traits)} traits")
    print(f"distilled {total} traits across {len(session_ids)} sessions")
    return 0


async def _cmd_list(args: argparse.Namespace) -> int:
    kind: TraitKind | None = (
        TraitKind(args.kind) if args.kind else None
    )
    store = await UserModelStore.open_or_create(args.db_path)
    async with store as s:
        traits = await s.list_traits_for_user(
            args.user_id,
            kind=kind,
            min_confidence=args.min_confidence,
            tenant_id=args.tenant_id,
        )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "user_id": t.user_id,
                        "kind": t.trait_kind.value,
                        "value": t.trait_value,
                        "confidence": t.confidence,
                        "first_seen": t.first_seen,
                        "last_seen": t.last_seen,
                        "session_ids": list(t.session_ids),
                    }
                    for t in traits
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if not traits:
        print(f"no traits for {args.user_id}")
        return 0
    print(f"{len(traits)} trait(s) for {args.user_id}:")
    for t in traits:
        print(
            f"  {t.trait_kind.value:10s} {t.confidence:0.2f}  {t.trait_value}"
        )
    return 0


async def _cmd_prune(args: argparse.Namespace) -> int:
    store = await UserModelStore.open_or_create(args.db_path)
    async with store as s:
        deleted = await s.prune_low_confidence(args.confidence_floor)
    print(f"pruned {deleted} trait(s) below confidence {args.confidence_floor}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_recent_session_ids(sessions_db_path: Path, cutoff_ms: int) -> list[str]:
    """Find every session_key whose latest turn is newer than ``cutoff_ms``.

    The Rust gateway writes ``ts`` as RFC3339 strings, not unix ms, so
    we filter in Python after parsing. Cheap on a real-sized DB; if it
    ever isn't, the right fix is to add a unix-ms column on the Rust
    side, not paper over it here.
    """
    if not sessions_db_path.exists():
        return []
    uri = f"file:{sessions_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT session_key, MAX(ts) FROM sessions GROUP BY session_key"
        ).fetchall()
    finally:
        conn.close()

    out: list[str] = []
    for session_key, ts in rows:
        ts_ms = _rfc3339_to_ms(str(ts)) if ts is not None else None
        if ts_ms is not None and ts_ms >= cutoff_ms:
            out.append(str(session_key))
    return out


def _rfc3339_to_ms(ts: str) -> int | None:
    """Parse RFC3339 (with trailing ``Z``) to unix ms.

    Returns ``None`` on malformed input — the caller treats it as "too
    old" and skips the session, which is the safe default.
    """
    from datetime import datetime

    try:
        # Python 3.11+ accepts trailing 'Z' via fromisoformat.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1_000)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_caller: LLMCaller | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    caller = llm_caller or default_llm_caller

    if args.command == "distill-once":
        return asyncio.run(_cmd_distill_once(args, caller))
    if args.command == "distill-recent":
        return asyncio.run(_cmd_distill_recent(args, caller))
    if args.command == "list":
        return asyncio.run(_cmd_list(args))
    if args.command == "prune":
        return asyncio.run(_cmd_prune(args))

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
