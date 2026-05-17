"""``corlinman replay`` — deterministic session replay.

Python port of ``rust/crates/corlinman-cli/src/cmd/replay.rs``. Direct
delegate to :func:`corlinman_replay.replay`.

Output modes mirror the Rust CLI:

* ``--output human`` (default): chat-style transcript with role labels
  + timestamps; suited for a terminal review.
* ``--output json``: pretty-printed JSON in the same shape the
  ``/admin/sessions/:key/replay`` HTTP route emits — pipe-friendly for
  ``corlinman replay X --output json | jq ...`` debugging flows.

Tenant scoping: ``--tenant-id`` (defaults to ``default``) drives reads
from ``<data_dir>/tenants/<tenant>/sessions.sqlite``. Slug shape is
validated via :class:`corlinman_replay.TenantId.new`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import click

from corlinman_server.cli._common import resolve_data_dir


@click.command("replay")
@click.argument("session_id")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the data directory. Defaults to $CORLINMAN_DATA_DIR or ~/.corlinman.",
)
@click.option(
    "--mode",
    type=click.Choice(["transcript", "rerun"], case_sensitive=False),
    default="transcript",
    show_default=True,
    help="Replay mode. `transcript` is the read-only dump; `rerun` is the Wave 2.5 stub.",
)
@click.option(
    "--output",
    type=click.Choice(["human", "json"], case_sensitive=False),
    default="human",
    show_default=True,
    help="Output format. `human` is terminal-friendly; `json` matches the HTTP wire shape.",
)
@click.option(
    "--tenant-id",
    default="default",
    show_default=True,
    help="Tenant slug (validated via TenantId.new).",
)
def replay(
    session_id: str,
    data_dir: Path | None,
    mode: str,
    output: str,
    tenant_id: str,
) -> None:
    """Reconstruct a stored session by key from ``sessions.sqlite``."""
    asyncio.run(
        _run(
            session_id=session_id,
            data_dir=data_dir,
            mode=mode.lower(),
            output=output.lower(),
            tenant_id=tenant_id,
        )
    )


async def _run(
    *,
    session_id: str,
    data_dir: Path | None,
    mode: str,
    output: str,
    tenant_id: str,
) -> None:
    from corlinman_replay import (
        ReplayMode,
        SessionNotFoundError,
        TenantId,
        TenantIdError,
        replay as replay_fn,
    )

    try:
        tenant = TenantId.new(tenant_id)
    except TenantIdError as exc:
        click.echo(f"error: invalid --tenant-id {tenant_id!r}: {exc}", err=True)
        sys.exit(1)

    dd = resolve_data_dir(data_dir)
    replay_mode = ReplayMode.RERUN if mode == "rerun" else ReplayMode.TRANSCRIPT

    try:
        out = await replay_fn(dd, tenant, session_id, replay_mode)
    except SessionNotFoundError:
        click.echo(
            f"error: session not found: {session_id!r} under tenant "
            f"{tenant.as_str()!r} (data dir {dd})",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — surface store errors verbatim
        click.echo(f"error: replay failed: {exc}", err=True)
        sys.exit(1)

    if output == "json":
        _print_json(out)
    else:
        _print_human(out)


def _print_human(out: object) -> None:
    summary = getattr(out, "summary", None)
    mc = getattr(summary, "message_count", 0) if summary is not None else 0
    tid = getattr(summary, "tenant_id", "default") if summary is not None else "default"
    mode = getattr(out, "mode", "transcript")
    session_key = getattr(out, "session_key", "")
    click.echo(
        f"session: {session_key} · tenant: {tid} · mode: {mode} · {mc} message(s)"
    )
    rerun_diff = getattr(summary, "rerun_diff", None) if summary is not None else None
    if rerun_diff:
        click.echo(f"rerun: {rerun_diff} (Wave 2.5 deferred)")
    click.echo("")

    transcript = getattr(out, "transcript", []) or []
    for i, msg in enumerate(transcript, start=1):
        role = getattr(msg, "role", "")
        label = {
            "user": "USER",
            "assistant": "ASSISTANT",
            "system": "SYSTEM",
            "tool": "TOOL",
        }.get(role, role)
        ts = getattr(msg, "ts", "")
        click.echo(f"[{i:>3}] {label} · {ts}")
        for line in (getattr(msg, "content", "") or "").splitlines():
            click.echo(f"    {line}")
        click.echo("")


def _print_json(out: object) -> None:
    try:
        payload = asdict(out)  # type: ignore[arg-type]
    except TypeError:
        # ReplayOutput is a slots dataclass — asdict should always work,
        # but fall back to attribute scrape just in case.
        payload = {
            "session_key": getattr(out, "session_key", ""),
            "mode": getattr(out, "mode", ""),
            "transcript": [
                {
                    "role": getattr(m, "role", ""),
                    "content": getattr(m, "content", ""),
                    "ts": getattr(m, "ts", ""),
                }
                for m in getattr(out, "transcript", []) or []
            ],
            "summary": {
                "message_count": getattr(getattr(out, "summary", None), "message_count", 0),
                "tenant_id": getattr(getattr(out, "summary", None), "tenant_id", "default"),
                "rerun_diff": getattr(getattr(out, "summary", None), "rerun_diff", None),
            },
        }
    click.echo(json.dumps(payload, indent=2, default=str))


__all__ = ["replay"]
