"""``corlinman doctor`` — diagnostic checks.

Python port of ``rust/crates/corlinman-cli/src/cmd/doctor/mod.rs``.

The Rust crate maintains a large suite of checks (provider HTTPS,
manifest duplicates, scheduler, etc.); the Python port ships a smaller
beachhead set that the Python AI plane can introspect without reaching
into the Rust gateway's config:

* ``data_dir`` — does the data directory exist and is it writable?
* ``config`` — is ``<data_dir>/config.toml`` present?
* ``python`` — interpreter version sanity check (matches the
  ``requires-python = ">=3.12"`` constraint).
* ``packages`` — import smoke for the workspace siblings the AI plane
  depends on at runtime.

Each check returns ``(status, message, hint)`` where ``status`` is one
of ``ok|warn|fail``. Only ``fail`` flips the exit code, matching the
Rust contract ("warnings are informational — we don't want
``doctor`` in a CI loop to fail just because the user hasn't configured
a provider yet").
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from corlinman_server.cli._common import echo_json, resolve_data_dir


@dataclass(slots=True)
class CheckReport:
    """Single check result. Wire shape matches the Rust
    ``CheckReport`` JSON serialisation: ``{name, status, message, hint?}``.
    """

    name: str
    status: str  # "ok" | "warn" | "fail"
    message: str
    hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.hint is not None:
            out["hint"] = self.hint
        return out


def _check_data_dir(data_dir: Path) -> CheckReport:
    if not data_dir.exists():
        return CheckReport(
            name="data_dir",
            status="warn",
            message=f"{data_dir} does not exist",
            hint="run `corlinman onboard --non-interactive --accept-risk`",
        )
    if not os.access(data_dir, os.W_OK):
        return CheckReport(
            name="data_dir",
            status="fail",
            message=f"{data_dir} is not writable",
            hint="check ownership / permissions",
        )
    return CheckReport(
        name="data_dir",
        status="ok",
        message=str(data_dir),
    )


def _check_config(data_dir: Path) -> CheckReport:
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="config",
            status="warn",
            message=f"{config_path} not present",
            hint="run `corlinman onboard` or `corlinman config init`",
        )
    return CheckReport(
        name="config",
        status="ok",
        message=str(config_path),
    )


def _check_python() -> CheckReport:
    info = sys.version_info
    if info < (3, 12):
        return CheckReport(
            name="python",
            status="fail",
            message=f"python {info.major}.{info.minor} < 3.12 (requires-python)",
            hint="install python 3.12 or newer",
        )
    return CheckReport(
        name="python",
        status="ok",
        message=f"python {info.major}.{info.minor}.{info.micro}",
    )


# Workspace packages the AI plane imports at runtime. Each is asserted
# importable so a missing dep flag-and-fails fast — much friendlier than
# the gRPC server blowing up at boot with an `ImportError`.
_REQUIRED_PACKAGES: tuple[str, ...] = (
    "corlinman_server",
    "corlinman_providers",
    "corlinman_replay",
)


def _check_packages() -> CheckReport:
    missing: list[str] = []
    for pkg in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
        except Exception:  # noqa: BLE001 — any import failure is a fail
            missing.append(pkg)
    if missing:
        return CheckReport(
            name="packages",
            status="fail",
            message=f"missing imports: {', '.join(missing)}",
            hint="run `uv sync` in the repo root",
        )
    return CheckReport(
        name="packages",
        status="ok",
        message=f"all {len(_REQUIRED_PACKAGES)} required packages importable",
    )


# Registered checks; keep the names stable so ``--module`` filtering is
# scriptable. Insertion order is the display order.
_CHECK_FNS = {
    "data_dir": lambda dd: _check_data_dir(dd),
    "config": lambda dd: _check_config(dd),
    "python": lambda _dd: _check_python(),
    "packages": lambda _dd: _check_packages(),
}


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human-readable output.")
@click.option(
    "--module",
    "module",
    default=None,
    help="Run a single check by name (e.g. `data_dir`, `config`, `python`, `packages`).",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data-dir (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def doctor(as_json: bool, module: str | None, data_dir: Path | None) -> None:
    """Run diagnostic checks across data-dir / config / runtime."""
    dd = resolve_data_dir(data_dir)

    items = list(_CHECK_FNS.items())
    if module is not None:
        items = [(name, fn) for name, fn in items if name == module]
        if not items:
            click.echo(f"error: no check named '{module}'", err=True)
            sys.exit(2)

    reports = [fn(dd) for _name, fn in items]

    if as_json:
        echo_json([r.to_dict() for r in reports])
    else:
        _print_human(reports)

    if any(r.status == "fail" for r in reports):
        sys.exit(1)


def _print_human(reports: list[CheckReport]) -> None:
    name_w = max((len(r.name) for r in reports), default=0)
    name_w = max(name_w, 8)
    fails = warns = oks = 0
    for r in reports:
        if r.status == "ok":
            glyph = "✓"
            oks += 1
        elif r.status == "warn":
            glyph = "!"
            warns += 1
        else:
            glyph = "✗"
            fails += 1
        click.echo(f"{glyph} {r.name:<{name_w}}  {r.message}")
        if r.hint:
            click.echo(f"  {'':<{name_w}}  hint: {r.hint}")
    click.echo("")
    click.echo(f"{fails} fail, {warns} warn, {oks} ok")


__all__ = ["doctor", "CheckReport"]
