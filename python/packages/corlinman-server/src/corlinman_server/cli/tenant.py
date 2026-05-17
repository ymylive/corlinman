"""``corlinman tenant`` — multi-tenant admin (Python port of
``rust/crates/corlinman-cli/src/cmd/tenant.rs``).

Two subcommands:

* ``corlinman tenant create <slug>`` — register a new tenant in
  ``<data_dir>/tenants.sqlite``, create the per-tenant directory
  tree under ``<data_dir>/tenants/<slug>/``, and seed an admin
  credential row in ``tenant_admins``.
* ``corlinman tenant list`` — print the current tenant roster.

Slug validation, admin DB schema, and the per-tenant directory layout
all come from :mod:`corlinman_server.tenancy`, which is the Python port
of the Rust ``corlinman-tenant`` crate.

Password hashing uses argon2id (matching the gateway's existing
``$argon2id$v=19$...`` admin credential format). When stdin is a TTY
and ``--admin-password`` is not given, we prompt via :mod:`getpass` so
the password isn't echoed to the terminal or shell history; otherwise
we read a single line from stdin (scripts can pipe a password in).
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
import time
from pathlib import Path

import click

from corlinman_server.cli._common import resolve_data_dir


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _format_unix_ms(ms: int) -> str:
    """Format a unix-ms timestamp as RFC-3339. Mirrors the Rust port,
    which uses ``time::OffsetDateTime`` for the same effect."""
    try:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        # ``isoformat`` emits "+00:00" — swap to "Z" to match
        # ``well_known::Rfc3339`` output from the Rust side.
        out = dt.isoformat(timespec="seconds")
        return out.replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def _hash_password(password: str) -> str:
    """Argon2id PHC-formatted hash. Uses :mod:`argon2-cffi` when
    available; falls back to :mod:`passlib` or a stdlib-only message if
    neither lib is installed. The Rust gateway only accepts
    ``$argon2id$`` prefixed strings.
    """
    try:
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)
    except ImportError:
        pass
    try:
        from passlib.hash import argon2

        return argon2.using(type="ID").hash(password)
    except ImportError:
        click.echo(
            "error: argon2id not available — install `argon2-cffi` or `passlib`",
            err=True,
        )
        sys.exit(2)


def _prompt_password(prompt: str) -> str:
    if sys.stdin.isatty() and sys.stderr.isatty():
        try:
            return getpass.getpass(prompt)
        except (KeyboardInterrupt, EOFError):
            click.echo("\nerror: password prompt cancelled", err=True)
            sys.exit(1)
    # Non-TTY: read a single line. Use stderr for the prompt so stdout
    # stays clean for downstream pipes.
    click.echo(prompt, nl=False, err=True)
    line = sys.stdin.readline()
    return line.rstrip("\r\n")


# ----------------------------------------------------------------------
# click group + commands
# ----------------------------------------------------------------------


@click.group("tenant", help="Multi-tenant admin (create / list).")
def tenant() -> None:
    """``tenant`` subcommand group."""


@tenant.command("create")
@click.argument("slug")
@click.option(
    "--display-name",
    default=None,
    help="Human-readable display name shown in the admin UI. Defaults to the slug.",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the data directory. Defaults to $CORLINMAN_DATA_DIR or ~/.corlinman.",
)
@click.option(
    "--admin-username",
    required=True,
    help="Initial admin username for this tenant.",
)
@click.option(
    "--admin-password",
    default=None,
    help="Plaintext password. Omit to prompt on stdin (echo disabled when a TTY).",
)
def create_cmd(
    slug: str,
    display_name: str | None,
    data_dir: Path | None,
    admin_username: str,
    admin_password: str | None,
) -> None:
    """Create a new tenant: register in ``tenants.sqlite``, create the
    per-tenant data dir, and seed an admin credential."""
    asyncio.run(
        _run_create(
            slug=slug,
            display_name=display_name,
            data_dir=data_dir,
            admin_username=admin_username,
            admin_password=admin_password,
        )
    )


async def _run_create(
    *,
    slug: str,
    display_name: str | None,
    data_dir: Path | None,
    admin_username: str,
    admin_password: str | None,
) -> None:
    from corlinman_server.tenancy import (
        AdminDb,
        AdminExistsError,
        TenantExistsError,
        TenantId,
        TenantIdError,
        tenant_root_dir,
    )

    try:
        tenant_id = TenantId.new(slug)
    except TenantIdError as exc:
        click.echo(f"error: invalid tenant slug '{slug}': {exc}", err=True)
        sys.exit(1)

    dd = resolve_data_dir(data_dir)
    try:
        dd.mkdir(parents=True, exist_ok=True)
        tenant_dir = tenant_root_dir(dd, tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        click.echo(f"error: create tenant dir: {exc}", err=True)
        sys.exit(1)

    display = display_name or slug
    admin_db_path = dd / "tenants.sqlite"

    db = await AdminDb.open(admin_db_path)
    try:
        try:
            await db.create_tenant(tenant_id, display, _now_unix_ms())
        except TenantExistsError as exc:
            click.echo(
                f"error: tenant '{slug}' already exists in {admin_db_path}: {exc}",
                err=True,
            )
            sys.exit(1)

        password = admin_password if admin_password is not None else _prompt_password(
            f"admin password for tenant '{tenant_id.as_str()}': "
        )
        if not password:
            click.echo("error: admin password must not be empty", err=True)
            sys.exit(1)
        password_hash = _hash_password(password)

        try:
            await db.add_admin(tenant_id, admin_username, password_hash, _now_unix_ms())
        except AdminExistsError as exc:
            click.echo(
                f"error: admin '{admin_username}' already exists for tenant '{slug}': {exc}",
                err=True,
            )
            sys.exit(1)
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    click.echo(
        f"created tenant '{tenant_id.as_str()}' ({display}) with admin '{admin_username}'"
    )
    click.echo(f"  data dir : {tenant_dir}")
    click.echo(f"  admin db : {admin_db_path}")


@tenant.command("list")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the data directory. Defaults to $CORLINMAN_DATA_DIR or ~/.corlinman.",
)
def list_cmd(data_dir: Path | None) -> None:
    """List the current tenant roster."""
    asyncio.run(_run_list(data_dir=data_dir))


async def _run_list(*, data_dir: Path | None) -> None:
    from corlinman_server.tenancy import AdminDb

    dd = resolve_data_dir(data_dir)
    admin_db_path = dd / "tenants.sqlite"

    if not admin_db_path.exists():
        click.echo(f"(no tenants.sqlite at {admin_db_path})")
        click.echo("  run `corlinman tenant create <slug>` first")
        return

    db = await AdminDb.open(admin_db_path)
    try:
        rows = await db.list_active()
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass

    if not rows:
        click.echo("(no tenants registered)")
        return

    # Lightweight ASCII table — no third-party dep required.
    headers = ("TENANT ID", "DISPLAY NAME", "CREATED")
    body = [
        (
            r.tenant_id.as_str(),
            r.display_name,
            _format_unix_ms(r.created_at),
        )
        for r in rows
    ]
    widths = [max(len(h), max(len(row[i]) for row in body)) for i, h in enumerate(headers)]
    sep = "  ".join("-" * w for w in widths)
    click.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo(sep)
    for row in body:
        click.echo("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


__all__ = ["tenant"]
