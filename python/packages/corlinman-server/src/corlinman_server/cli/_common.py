"""Shared helpers for the ``corlinman`` CLI subcommands.

Centralises data-dir resolution + the "not yet ported" stub so each
subcommand module stays focused on its own argument surface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

import click

#: Environment variable consulted when ``--data-dir`` is not provided.
ENV_DATA_DIR = "CORLINMAN_DATA_DIR"

#: Subdirectories created by ``corlinman onboard``. Mirrors the Rust
#: ``SUBDIRS`` constant in ``cmd/onboard.rs``.
SUBDIRS: tuple[str, ...] = ("agents", "plugins", "knowledge", "vector", "logs")


def resolve_data_dir(explicit: Path | None) -> Path:
    """Resolve the data directory in the same order as the Rust CLI:

    1. explicit ``--data-dir`` flag (when not ``None``)
    2. ``$CORLINMAN_DATA_DIR``
    3. ``~/.corlinman``
    """
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def default_config_path(data_dir: Path | None = None) -> Path:
    """Return the default config path: ``<data_dir>/config.toml``."""
    return resolve_data_dir(data_dir) / "config.toml"


def todo_stub(name: str, exit_code: int = 2) -> NoReturn:
    """Print the standard "not yet ported" message and exit.

    Used by every subcommand module that still needs an implementation —
    keeps the dispatch tree complete so ``corlinman --help`` lists every
    Rust subcommand even when the body is a placeholder.
    """
    click.echo(
        f"TODO: not yet ported in Python migration: {name}",
        err=True,
    )
    sys.exit(exit_code)


def echo_json(payload: object, *, pretty: bool = True) -> None:
    """Emit ``payload`` to stdout as JSON.

    Pretty by default to match the Rust CLI's
    ``serde_json::to_string_pretty`` choice for ``--json`` modes;
    callers that want compact output pass ``pretty=False``.
    """
    import json

    if pretty:
        click.echo(json.dumps(payload, indent=2, sort_keys=False, default=str))
    else:
        click.echo(json.dumps(payload, separators=(",", ":"), default=str))
