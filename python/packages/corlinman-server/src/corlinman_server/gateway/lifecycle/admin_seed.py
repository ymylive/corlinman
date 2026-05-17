"""First-boot admin credential seeding.

When the gateway boots without an ``[admin]`` block in its config TOML
(or with an empty one), we seed the default operator account
``admin`` / ``root`` and persist it so the UI is immediately usable.
The seeded state carries ``must_change_password=True``; the UI checks
that flag on login and force-redirects to ``/account/security`` so the
operator picks a real password before doing anything else.

This module is intentionally narrow:

* it only touches the ``[admin]`` block of the on-disk TOML;
* it never reads or rewrites other config sections — operators that
  hand-edit their config file see the rest of it preserved verbatim;
* the hashing path goes through
  :func:`corlinman_server.gateway.routes_admin_a.auth.hash_password`
  so the argon2 params stay in lockstep with the rotate endpoint.

The contract is async because the entrypoint already runs inside an
event loop when constructing the admin state, but the disk IO itself
is synchronous (the TOML is tiny). We wrap the write in
``asyncio.to_thread`` so the loop stays responsive.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


# Defaults used when no ``[admin]`` block is present in the config.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "root"  # noqa: S105 — documented bootstrap default


@dataclass(frozen=True)
class SeededAdmin:
    """Wire shape returned by :func:`ensure_admin_credentials`."""

    username: str
    password_hash: str
    config_path: Path
    must_change_password: bool
    seeded_now: bool  # True if we just wrote the default credentials


def _hash_password(plaintext: str) -> str:
    """Argon2id hash via the same hasher used by the rotate route."""
    # Imported lazily so ``admin_seed`` stays usable in tests that
    # haven't installed argon2-cffi yet (the function only runs on the
    # real boot path).
    from corlinman_server.gateway.routes_admin_a.auth import hash_password

    return hash_password(plaintext)


def _parse_admin_block(text: str) -> dict[str, str | bool] | None:
    """Return the parsed ``[admin]`` table or ``None`` if absent.

    Uses ``tomllib`` (3.11+) so we don't depend on the broader config
    schema. Malformed TOML is treated as "no admin block" — the seed
    will then run and overwrite the bad block.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — Python <3.11
        return None
    try:
        parsed = tomllib.loads(text)
    except Exception:  # noqa: BLE001 — operator typo'd the file
        return None
    admin = parsed.get("admin")
    if not isinstance(admin, dict):
        return None
    out: dict[str, str | bool] = {}
    user = admin.get("username")
    pw = admin.get("password_hash")
    if isinstance(user, str) and user:
        out["username"] = user
    if isinstance(pw, str) and pw:
        out["password_hash"] = pw
    mcp = admin.get("must_change_password")
    if isinstance(mcp, bool):
        out["must_change_password"] = mcp
    return out or None


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_admin_block(
    *, username: str, password_hash: str, must_change_password: bool
) -> str:
    """Render the minimal ``[admin]`` TOML fragment we persist."""
    return (
        "[admin]\n"
        f'username = "{_toml_escape(username)}"\n'
        f'password_hash = "{_toml_escape(password_hash)}"\n'
        f"must_change_password = {'true' if must_change_password else 'false'}\n"
    )


def _merge_admin_block(existing: str, new_block: str) -> str:
    """Replace (or append) the ``[admin]`` block in ``existing`` TOML.

    We do a lightweight section-level swap rather than a full TOML
    round-trip — the gateway's config TOML mixes hand-edited comments
    with generated sections and a full re-emit would lose them. The
    section boundary is the next ``[...]`` header at column 0 or EOF.
    """
    lines = existing.splitlines(keepends=True)
    if not lines:
        return new_block

    # Find the [admin] header (any case, exact match).
    start: int | None = None
    end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[admin]":
            start = i
            # Find the next section header or EOF.
            for j in range(i + 1, len(lines)):
                lj = lines[j].strip()
                if lj.startswith("[") and lj.endswith("]"):
                    end = j
                    break
            if end is None:
                end = len(lines)
            break
    if start is None:
        # No existing [admin] — append. Ensure a trailing newline first.
        prefix = existing if existing.endswith("\n") else existing + "\n"
        return prefix + "\n" + new_block
    # Splice the new block in over the old range.
    return "".join(lines[:start]) + new_block + "".join(lines[end:])


async def _atomic_write(path: Path, contents: str) -> None:
    """``<path>.new`` then ``os.replace`` — same pattern as auth.py."""

    def _do() -> None:
        parent = path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, path)

    await asyncio.to_thread(_do)


def resolve_admin_config_path(
    *, cli_config_path: Path | None, data_dir: Path
) -> Path:
    """Pick the file we'll read/write the ``[admin]`` block from.

    Preference order:

    1. The ``--config`` path the operator passed on the CLI (if any).
    2. ``<data_dir>/config.toml`` — what first-boot installs default to.

    The file is *not* required to exist; the seed routine will create
    it. Returning a concrete path means the rotate endpoint always has
    somewhere to persist subsequent changes (no more 503
    ``config_path_unset`` on fresh installs).
    """
    if cli_config_path is not None:
        return cli_config_path
    return data_dir / "config.toml"


async def ensure_admin_credentials(
    *,
    config_path: Path,
) -> SeededAdmin:
    """Read the admin block from ``config_path``; seed defaults if absent.

    Returns the resolved credentials + a flag indicating whether this
    call wrote new defaults (so the entrypoint can log the loud "we
    just installed default credentials, please rotate" warning).
    """
    existing_text = ""
    parsed: dict[str, str | bool] | None = None
    if config_path.exists():
        try:
            existing_text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "admin_seed.read_failed", path=str(config_path), error=str(exc)
            )
        else:
            parsed = _parse_admin_block(existing_text)

    if parsed is not None and "username" in parsed and "password_hash" in parsed:
        # Operator-provided credentials win — never overwrite.
        must_change = bool(parsed.get("must_change_password", False))
        return SeededAdmin(
            username=str(parsed["username"]),
            password_hash=str(parsed["password_hash"]),
            config_path=config_path,
            must_change_password=must_change,
            seeded_now=False,
        )

    # No usable admin block — seed the defaults and persist.
    password_hash = _hash_password(DEFAULT_ADMIN_PASSWORD)
    new_block = _render_admin_block(
        username=DEFAULT_ADMIN_USERNAME,
        password_hash=password_hash,
        must_change_password=True,
    )
    merged = _merge_admin_block(existing_text, new_block)
    try:
        await _atomic_write(config_path, merged)
    except OSError as exc:
        # We still return the in-memory credentials so the gateway can
        # boot; the rotate endpoint will retry the write the next time
        # the operator changes their password.
        logger.warning(
            "admin_seed.write_failed", path=str(config_path), error=str(exc)
        )

    logger.warning(
        "admin_seed.default_credentials_installed",
        path=str(config_path),
        username=DEFAULT_ADMIN_USERNAME,
        hint=(
            "first-boot defaults are admin/root; the UI will force a "
            "password rotation on first login"
        ),
    )
    return SeededAdmin(
        username=DEFAULT_ADMIN_USERNAME,
        password_hash=password_hash,
        config_path=config_path,
        must_change_password=True,
        seeded_now=True,
    )


__all__ = [
    "DEFAULT_ADMIN_PASSWORD",
    "DEFAULT_ADMIN_USERNAME",
    "SeededAdmin",
    "ensure_admin_credentials",
    "resolve_admin_config_path",
]
