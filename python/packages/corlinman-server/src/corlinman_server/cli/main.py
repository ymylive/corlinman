"""``corlinman`` CLI entry point — Python port of ``corlinman-cli/main.rs``.

Wires the root click group and dispatches every subcommand from the Rust
binary. ``main`` is the ``[project.scripts]`` console-script target
(``corlinman = "corlinman_server.cli.main:main"``).

Mirrors the Rust subcommand tree 1:1:

    onboard | doctor | plugins | config | dev | qa | vector |
    tenant  | replay | migrate | rollback | skills | identity

For commands that aren't fully ported yet, the click handler defers to
:func:`corlinman_server.cli._common.todo_stub` which prints
``TODO: not yet ported in Python migration`` to stderr and exits 2.

The dispatch tree exposes every Rust subcommand (FULL or STUB) so
``corlinman --help`` lists the full surface — operators trying to
script against the Rust CLI see the same names here even if the body
is a placeholder.
"""

from __future__ import annotations

import click

from corlinman_server.cli._common import todo_stub
from corlinman_server.cli.config import config as config_group
from corlinman_server.cli.doctor import doctor as doctor_cmd
from corlinman_server.cli.onboard import onboard as onboard_cmd
from corlinman_server.cli.plugins import plugins as plugins_group
from corlinman_server.cli.replay import replay as replay_cmd
from corlinman_server.cli.tenant import tenant as tenant_group


@click.group(
    name="corlinman",
    help=(
        "corlinman — self-hosted LLM toolbox (Python).\n"
        "\n"
        "Subcommands marked STUB exit 2 with a ``TODO: not yet ported`` message."
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(package_name="corlinman-server", prog_name="corlinman")
def cli() -> None:
    """Root group; subcommands attached below."""


# --- FULL ports -----------------------------------------------------------

cli.add_command(onboard_cmd)
cli.add_command(doctor_cmd)
cli.add_command(plugins_group)
cli.add_command(config_group)
cli.add_command(tenant_group)
cli.add_command(replay_cmd)


# --- STUB ports -----------------------------------------------------------
#
# Each stub mirrors the Rust subcommand surface at the *name* level so
# ``corlinman --help`` lists every command. The body is a single
# ``todo_stub`` call that prints the canonical "not yet ported" line and
# exits 2.


@cli.group(
    name="dev",
    help="Developer helpers (watch / gen-proto / check). STUB — not yet ported.",
)
def dev_group() -> None:  # pragma: no cover - stub dispatcher
    """Dev helpers (stub)."""


@dev_group.command("watch")
def _dev_watch() -> None:  # pragma: no cover - stub
    todo_stub("dev watch")


@dev_group.command("gen-proto")
def _dev_gen_proto() -> None:  # pragma: no cover - stub
    todo_stub("dev gen-proto")


@dev_group.command("check")
def _dev_check() -> None:  # pragma: no cover - stub
    todo_stub("dev check")


@cli.group(
    name="qa",
    help="QA scenario runner + perf bench. STUB — not yet ported.",
)
def qa_group() -> None:  # pragma: no cover - stub dispatcher
    """QA (stub)."""


@qa_group.command("run")
@click.option("--scenarios-dir", default="qa/scenarios", show_default=True)
@click.option("--filter", "filter_", default=None)
@click.option("--include-live", is_flag=True)
def _qa_run(scenarios_dir: str, filter_: str | None, include_live: bool) -> None:  # pragma: no cover - stub
    todo_stub("qa run")


@qa_group.command("bench")
@click.option("--iterations", type=int, default=200, show_default=True)
@click.option("--warmup", type=int, default=20, show_default=True)
@click.option("--report", type=click.Path(), default=None)
def _qa_bench(iterations: int, warmup: int, report: str | None) -> None:  # pragma: no cover - stub
    todo_stub("qa bench")


@cli.group(
    name="vector",
    help="Vector index: stats / query / rebuild / namespaces. STUB — not yet ported.",
)
def vector_group() -> None:  # pragma: no cover - stub dispatcher
    """Vector (stub)."""


@vector_group.command("stats")
@click.option("--json", "as_json", is_flag=True)
@click.option("--path", type=click.Path(), default=None)
def _vector_stats(as_json: bool, path: str | None) -> None:  # pragma: no cover - stub
    todo_stub("vector stats")


@vector_group.command("query")
@click.argument("query")
@click.option("-k", "--top-k", type=int, default=10, show_default=True)
@click.option("--tag", multiple=True)
@click.option("--exclude", multiple=True)
@click.option("--json", "as_json", is_flag=True)
@click.option("--path", type=click.Path(), default=None)
def _vector_query(
    query: str,
    top_k: int,
    tag: tuple[str, ...],
    exclude: tuple[str, ...],
    as_json: bool,
    path: str | None,
) -> None:  # pragma: no cover - stub
    todo_stub("vector query")


@vector_group.command("rebuild")
@click.option("--source", type=click.Path(), default=None)
@click.option("--confirm", is_flag=True)
@click.option("--path", type=click.Path(), default=None)
def _vector_rebuild(source: str | None, confirm: bool, path: str | None) -> None:  # pragma: no cover - stub
    todo_stub("vector rebuild")


@vector_group.command("namespaces")
@click.option("--json", "as_json", is_flag=True)
@click.option("--path", type=click.Path(), default=None)
def _vector_namespaces(as_json: bool, path: str | None) -> None:  # pragma: no cover - stub
    todo_stub("vector namespaces")


# --- Extra Python-only stub groups ---------------------------------------
#
# These don't have a Rust counterpart at the CLI layer but the
# ``cli/__init__.py`` module-level docstring promises them, and they have
# Python siblings ready for follow-up wiring.


@cli.group(
    name="migrate",
    help="Evolution-store migrations (corlinman_evolution_store). STUB — not yet ported.",
)
def migrate_group() -> None:  # pragma: no cover - stub dispatcher
    """Migrate (stub)."""


@migrate_group.command("status")
def _migrate_status() -> None:  # pragma: no cover - stub
    todo_stub("migrate status")


@migrate_group.command("up")
def _migrate_up() -> None:  # pragma: no cover - stub
    todo_stub("migrate up")


@cli.group(
    name="rollback",
    help="Auto-rollback run-once (corlinman_auto_rollback). STUB — not yet ported.",
)
def rollback_group() -> None:  # pragma: no cover - stub dispatcher
    """Rollback (stub)."""


@rollback_group.command("run-once")
def _rollback_run_once() -> None:  # pragma: no cover - stub
    todo_stub("rollback run-once")


@cli.group(
    name="skills",
    help="Skill registry list/inspect (corlinman_skills_registry). STUB — not yet ported.",
)
def skills_group() -> None:  # pragma: no cover - stub dispatcher
    """Skills (stub)."""


@skills_group.command("list")
def _skills_list() -> None:  # pragma: no cover - stub
    todo_stub("skills list")


@skills_group.command("inspect")
@click.argument("name")
def _skills_inspect(name: str) -> None:  # pragma: no cover - stub
    todo_stub("skills inspect")


@cli.group(
    name="identity",
    help="Channel-alias resolver inspect (corlinman_identity). STUB — not yet ported.",
)
def identity_group() -> None:  # pragma: no cover - stub dispatcher
    """Identity (stub)."""


@identity_group.command("resolve")
@click.argument("alias")
def _identity_resolve(alias: str) -> None:  # pragma: no cover - stub
    todo_stub("identity resolve")


def main() -> None:
    """Console-script entry. Delegates to the click root group."""
    cli()


if __name__ == "__main__":  # pragma: no cover - direct execution
    main()


__all__ = ["cli", "main"]
