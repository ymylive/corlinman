"""``corlinman plugins {list,inspect,doctor}`` — plugin introspection.

Python port of ``rust/crates/corlinman-cli/src/cmd/plugins.rs``.

Backed by :mod:`corlinman_providers.plugins` — the registry is built
from ``$CORLINMAN_PLUGIN_DIRS`` exactly like the Rust version. ``list``
and ``inspect`` are manifest-only (no plugin code is executed);
``doctor`` walks every plugin and reports manifest-level issues.

``install`` / ``uninstall`` are stubs that exit 2 — the Rust binary
doesn't ship them either, but the task brief calls them out as
priority-FULL slots so we register the names for future wiring. The
``invoke`` subcommand from the Rust CLI is left as a stub too: the
JSON-RPC stdio runner lives in a Rust-side module that the Python
plane doesn't speak to directly yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from corlinman_server.cli._common import echo_json, todo_stub

# Import lazily inside command bodies — the registry import touches
# manifests / TOML parsers and we want ``--help`` to be cheap.


def _load_registry():  # type: ignore[no-untyped-def]
    from corlinman_providers.plugins import (
        Origin,
        PluginRegistry,
        SearchRoot,
        roots_from_env_var,
    )

    roots: list[SearchRoot] = roots_from_env_var(
        "CORLINMAN_PLUGIN_DIRS", Origin.CONFIG if hasattr(Origin, "CONFIG") else Origin.Config  # type: ignore[attr-defined]
    )
    # Python registry doesn't require sort/dedup — discovery handles it —
    # but we sort to keep output stable across operating systems.
    roots.sort(key=lambda r: r.path)
    return PluginRegistry.from_roots(roots)


def _parse_origin(s: str):  # type: ignore[no-untyped-def]
    from corlinman_providers.plugins import Origin

    table: dict[str, object] = {}
    # Origin enum may be ``Origin.Bundled`` (Rust-style) or
    # ``Origin.BUNDLED`` (Python-IntEnum-style). Accept both
    # presentations and key the table accordingly.
    for attr in ("Bundled", "BUNDLED"):
        if hasattr(Origin, attr):
            table["bundled"] = getattr(Origin, attr)
            break
    for attr in ("Global", "GLOBAL"):
        if hasattr(Origin, attr):
            table["global"] = getattr(Origin, attr)
            table["user"] = getattr(Origin, attr)
            break
    for attr in ("Workspace", "WORKSPACE"):
        if hasattr(Origin, attr):
            table["workspace"] = getattr(Origin, attr)
            break
    for attr in ("Config", "CONFIG"):
        if hasattr(Origin, attr):
            table["config"] = getattr(Origin, attr)
            break
    key = s.lower()
    if key not in table:
        raise click.BadParameter(
            f"unknown --source '{s}' (expected bundled|global|user|workspace|config)"
        )
    return table[key]


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(n - 1, 0)] + "…"


@click.group("plugins", help="Plugin introspection: list / inspect / install / uninstall / doctor.")
def plugins() -> None:
    """``plugins`` subcommand group."""


@plugins.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit a compact JSON array instead of a human table.")
@click.option(
    "--source",
    default=None,
    help="Hide plugins whose origin is not at or above the given source rank "
    "(bundled | global | user | workspace | config).",
)
@click.option("--enabled", is_flag=True, help="Reserved (no-op today).")
def list_cmd(as_json: bool, source: str | None, enabled: bool) -> None:  # noqa: ARG001
    """List every discovered plugin (origin-ranked)."""
    try:
        registry = _load_registry()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: load plugin registry: {exc}", err=True)
        sys.exit(2)

    rows = list(registry.list())
    if source is not None:
        min_origin = _parse_origin(source)
        # Origin compare: prefer ``.rank`` attribute if present (Python),
        # else fall back to direct enum compare.
        def _rank(origin: object) -> int:
            r = getattr(origin, "rank", None)
            if callable(r):
                return int(r())
            if isinstance(r, int):
                return r
            return int(origin)  # type: ignore[arg-type]
        min_rank = _rank(min_origin)
        rows = [e for e in rows if _rank(e.origin) >= min_rank]

    if as_json:
        payload = [
            {
                "name": e.manifest.name,
                "version": getattr(e.manifest, "version", ""),
                "plugin_type": _plugin_type_str(e.manifest),
                "origin": _origin_str(e.origin),
                "manifest_path": str(e.manifest_path),
                "shadowed_count": e.shadowed_count,
                "tool_count": _tool_count(e.manifest),
            }
            for e in rows
        ]
        click.echo(json.dumps(payload, separators=(",", ":"), default=str))
        return

    if not rows:
        click.echo("(no plugins discovered; set CORLINMAN_PLUGIN_DIRS)")
        return

    click.echo(f"{'NAME':<32} {'VERSION':<10} {'TYPE':<10} {'ORIGIN':<10} PATH")
    for e in rows:
        click.echo(
            f"{_truncate(e.manifest.name, 32):<32} "
            f"{_truncate(getattr(e.manifest, 'version', ''), 10):<10} "
            f"{_plugin_type_str(e.manifest):<10} "
            f"{_origin_str(e.origin):<10} "
            f"{e.manifest_path}"
        )
    diags = registry.diagnostics()
    if diags:
        click.echo("", err=True)
        click.echo(f"{len(diags)} diagnostics:", err=True)
        for diag in diags:
            click.echo(f"  - {diag!r}", err=True)


@plugins.command("inspect")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Emit the manifest as JSON.")
def inspect_cmd(name: str, as_json: bool) -> None:
    """Show the resolved manifest for one plugin."""
    try:
        registry = _load_registry()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: load plugin registry: {exc}", err=True)
        sys.exit(2)

    entry = registry.get(name)
    if entry is None:
        click.echo(f"error: plugin '{name}' not found in registry", err=True)
        sys.exit(1)

    if as_json:
        # Manifest may be a dataclass — fall back to a manual dict
        # build if to-dict utilities are absent.
        try:
            from dataclasses import asdict, is_dataclass

            payload = asdict(entry.manifest) if is_dataclass(entry.manifest) else dict(entry.manifest.__dict__)
        except Exception:  # noqa: BLE001
            payload = {"name": entry.manifest.name}
        echo_json(payload, pretty=False)
        return

    m = entry.manifest
    click.echo(f"Name:         {m.name}")
    click.echo(f"Version:      {getattr(m, 'version', '')}")
    if getattr(m, "description", ""):
        click.echo(f"Description:  {m.description}")
    if getattr(m, "author", ""):
        click.echo(f"Author:       {m.author}")
    click.echo(f"Type:         {_plugin_type_str(m)}")
    click.echo(f"Origin:       {_origin_str(entry.origin)}")
    click.echo(f"ManifestPath: {entry.manifest_path}")
    entry_point = getattr(m, "entry_point", None)
    if entry_point is not None:
        click.echo(f"EntryPoint:   {getattr(entry_point, 'command', '')}")
        args = getattr(entry_point, "args", None) or []
        if args:
            click.echo(f"  args:       {args!r}")
    tools = _tools(m)
    if tools:
        click.echo("\nTools:")
        for t in tools:
            first = (getattr(t, "description", "") or "").splitlines()[0] if getattr(t, "description", "") else ""
            click.echo(f"  - {t.name}: {_truncate(first, 72)}")
    if entry.shadowed_count > 0:
        click.echo(f"\nShadowed: {entry.shadowed_count} lower-rank manifest(s)")


@plugins.command("install")
@click.argument("source")
def install_cmd(source: str) -> None:  # pragma: no cover - stub
    """Install a plugin (STUB — not yet ported)."""
    todo_stub(f"plugins install {source!r}")


@plugins.command("uninstall")
@click.argument("name")
def uninstall_cmd(name: str) -> None:  # pragma: no cover - stub
    """Uninstall a plugin (STUB — not yet ported)."""
    todo_stub(f"plugins uninstall {name!r}")


@plugins.command("invoke")
@click.argument("target")
@click.option("--args", "args_json", default=None)
@click.option("--timeout", type=int, default=None)
def invoke_cmd(target: str, args_json: str | None, timeout: int | None) -> None:  # pragma: no cover - stub
    """Invoke ``<plugin>.<tool>`` via JSON-RPC stdio (STUB — not yet ported)."""
    todo_stub(f"plugins invoke {target!r}")


@plugins.command("doctor")
@click.argument("name", required=False)
def doctor_cmd(name: str | None) -> None:
    """Run plugin-specific diagnostics (manifest + entry_point + registry)."""
    try:
        registry = _load_registry()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: load plugin registry: {exc}", err=True)
        sys.exit(2)

    if name is not None:
        entry = registry.get(name)
        if entry is None:
            click.echo(f"error: plugin '{name}' not found", err=True)
            sys.exit(1)
        entries = [entry]
    else:
        entries = list(registry.list())

    total_issues = 0
    for e in entries:
        issues: list[str] = []
        entry_point = getattr(e.manifest, "entry_point", None)
        cmd_str = getattr(entry_point, "command", "") if entry_point is not None else ""
        if not (cmd_str and cmd_str.strip()):
            issues.append("entry_point.command is empty")
        if not getattr(e.manifest, "version", "").strip():
            issues.append("manifest is missing `version`")
        tools = _tools(e.manifest)
        if not tools and _plugin_type_str(e.manifest).lower() != "service":
            issues.append("capabilities.tools is empty")
        click.echo(
            f"[{_origin_str(e.origin)}] {e.manifest.name}  -> {len(issues)} issue(s)"
        )
        for i in issues:
            click.echo(f"    - {i}")
        total_issues += len(issues)
    if total_issues > 0:
        sys.exit(1)


# --- helpers shared across commands --------------------------------------


def _plugin_type_str(manifest: object) -> str:
    pt = getattr(manifest, "plugin_type", None)
    if pt is None:
        return ""
    for attr in ("as_str", "value"):
        v = getattr(pt, attr, None)
        if callable(v):
            try:
                return str(v())
            except Exception:  # noqa: BLE001
                pass
        elif v is not None:
            return str(v)
    return str(pt)


def _origin_str(origin: object) -> str:
    for attr in ("as_str", "value", "name"):
        v = getattr(origin, attr, None)
        if callable(v):
            try:
                return str(v())
            except Exception:  # noqa: BLE001
                pass
        elif v is not None:
            return str(v)
    return str(origin)


def _tools(manifest: object) -> list[object]:
    caps = getattr(manifest, "capabilities", None)
    if caps is None:
        return []
    tools = getattr(caps, "tools", None) or []
    return list(tools)


def _tool_count(manifest: object) -> int:
    return len(_tools(manifest))


__all__ = ["plugins"]
